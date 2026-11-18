"""Model routing — select models based on provider and config."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import httpx

from free_code import __version__
from free_code.config import CONFIG_DIR, PROVIDER_MODELS, ensure_config_dir, load_config

MODELS_URL = "https://api.free.ai/v1/models"
MODELS_CACHE = CONFIG_DIR / "models.json"
MODELS_TTL = 6 * 3600

# Models available on Free.ai's OpenAI-compatible endpoint
# (https://api.free.ai/v1/chat/completions — ids are whitelisted server-side)
# Offline fallback only: `free-code models` reads the live list from /v1/models.
FREE_AI_MODELS = [
    {"id": "qwen7b", "name": "Qwen 3.6 35B", "type": "chat", "free": True},
    {"id": "qwen-coder", "name": "Qwen 3.6 35B (code)", "type": "code", "free": True},
    {"id": "qwen3-coder", "name": "Qwen 3.6 35B (code)", "type": "code", "free": True},
]


def get_model(config: Optional[Dict[str, Any]] = None) -> str:
    """Get the model ID to use based on config."""
    if config is None:
        config = load_config()
    model = config.get("model")
    if model:
        return model
    provider = config.get("provider", "free.ai")
    return PROVIDER_MODELS.get(provider, "qwen7b")


def list_models(provider: Optional[str] = None) -> List[Dict[str, Any]]:
    """List available models for a provider."""
    if provider is None or provider == "free.ai":
        return FREE_AI_MODELS
    # For BYOK providers, return a hint list
    hints = {
        "openai": [
            {"id": "gpt-4o", "name": "GPT-4o"},
            {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
            {"id": "o3-mini", "name": "o3-mini"},
        ],
        "anthropic": [
            {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
            {"id": "claude-haiku-3-5-20241022", "name": "Claude 3.5 Haiku"},
        ],
        "google": [
            {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
            {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        ],
        "openrouter": [
            {"id": "anthropic/claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
            {"id": "openai/gpt-4o", "name": "GPT-4o"},
            {"id": "google/gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
        ],
    }
    return hints.get(provider, [])


def tier(model: Dict[str, Any]) -> str:
    """'free' (explicit zero token price), 'self-hosted' (run on Free.ai's own GPUs,
    no per-token price listed, counts against the daily free allowance) or 'paid'."""
    if "free" in model and "pricing" not in model:
        return "free" if model["free"] else "paid"
    pricing = model.get("pricing")
    if pricing is None:
        return "self-hosted" if model.get("self_hosted") else "paid"
    try:
        zero = float(pricing.get("prompt", 0) or 0) == 0 and float(pricing.get("completion", 0) or 0) == 0
    except (TypeError, ValueError):
        zero = False
    if zero and model.get("self_hosted"):
        return "free"
    return "paid"


def is_free(model: Dict[str, Any]) -> bool:
    """No token price: usable on the free tier (within daily limits)."""
    return tier(model) in ("free", "self-hosted")


def _price(model: Dict[str, Any]) -> str:
    p = model.get("pricing") or {}
    try:
        a, b = float(p.get("prompt", 0)) * 1e6, float(p.get("completion", 0)) * 1e6
    except (TypeError, ValueError):
        return ""
    if a == 0 and b == 0:
        return ""
    return f"${a:g} in / ${b:g} out per 1M tokens"


def describe(model: Dict[str, Any]) -> str:
    t = tier(model)
    label = {"free": "free tier", "self-hosted": "free tier (self-hosted, daily limits)",
             "paid": "paid (needs a key with tokens)"}[t]
    extra = _price(model)
    return label + (f", {extra}" if extra else "")


def fetch_models(force: bool = False, client: Optional[httpx.Client] = None,
                 cache_path=None) -> List[Dict[str, Any]]:
    """Every model Free.ai serves (cached 6h). Falls back to the cache, then the built-in list."""
    cache_path = cache_path or MODELS_CACHE
    cached: Optional[Dict[str, Any]] = None
    try:
        cached = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cached = None
    if cached and not force and time.time() - float(cached.get("fetched_at", 0)) < MODELS_TTL:
        return cached.get("models", [])
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        r = client.get(MODELS_URL, headers={"User-Agent": f"free-code/{__version__}"}, timeout=10)
        r.raise_for_status()
        models = [m for m in r.json().get("models", []) if isinstance(m, dict) and m.get("id")]
        if models:
            try:
                ensure_config_dir()
                cache_path.write_text(json.dumps({"fetched_at": time.time(), "models": models}))
            except OSError:
                pass
            return models
    except (httpx.HTTPError, ValueError):
        pass
    finally:
        if own:
            client.close()
    if cached and cached.get("models"):
        return cached["models"]
    return list(FREE_AI_MODELS)


def filter_models(models: List[Dict[str, Any]], *, kind: Optional[str] = None,
                  free_only: bool = False, query: str = "") -> List[Dict[str, Any]]:
    out = []
    q = query.lower().strip()
    for m in models:
        if kind and m.get("type") != kind:
            continue
        if free_only and not is_free(m):
            continue
        if q and q not in str(m.get("id", "")).lower() and q not in str(m.get("name", "")).lower():
            continue
        out.append(m)
    # free first, then by id: the picker leads with what costs nothing
    return sorted(out, key=lambda m: (not is_free(m), str(m.get("id", ""))))


def find_model(models: List[Dict[str, Any]], model_id: str) -> Optional[Dict[str, Any]]:
    for m in models:
        if m.get("id") == model_id:
            return m
    return None
