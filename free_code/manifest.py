"""Server-side tool manifest: fetched, cached, schema-validated, merged.

The manifest lets Free.ai ship a new platform tool, fix a tool's description or
parameters, switch a broken tool off, or announce a new CLI release without a
CLI release. Trust model: HTTPS only, a hard size cap, strict schema
validation (``remote_tools.validate_spec``), tools are declarative data that
can only call ``/v1/`` Free.ai API paths, a manifest can never define or
replace a local tool (shell, file, git), and a manifest older than the cached
one is ignored. Offline, or on any failure, the built-in set is used.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from free_code import __version__
from free_code.builtin_manifest import BUILTIN_MANIFEST, SCHEMA
from free_code.config import CONFIG_DIR, ensure_config_dir
from free_code.remote_tools import SpecError, validate_spec

MANIFEST_URL = "https://free.ai/api/v1/code/manifest/"
CACHE_FILE = CONFIG_DIR / "manifest.json"
CACHE_TTL_SECONDS = 6 * 3600
MAX_MANIFEST_BYTES = 128 * 1024
MAX_TOOLS = 40
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}([abrc]+\d+)?$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class ManifestError(ValueError):
    """The manifest failed validation as a whole."""


def manifest_disabled(config: Optional[Dict[str, Any]] = None) -> bool:
    if os.environ.get("FREE_CODE_NO_MANIFEST"):
        return True
    return bool(config) and config.get("manifest") is False


def manifest_url() -> str:
    url = os.environ.get("FREE_CODE_MANIFEST_URL") or MANIFEST_URL
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return MANIFEST_URL
    return url


def validate_manifest(data: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Return (clean manifest, list of per-tool rejection notes)."""
    if not isinstance(data, dict):
        raise ManifestError("manifest must be an object")
    if data.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported manifest schema {data.get('schema')!r}")
    mv = data.get("manifest_version")
    if not isinstance(mv, int) or isinstance(mv, bool) or mv < 1:
        raise ManifestError("manifest_version must be a positive integer")
    clean: Dict[str, Any] = {"schema": SCHEMA, "manifest_version": mv}
    for key in ("latest_version", "min_version"):
        v = data.get(key)
        if v is not None and (not isinstance(v, str) or not VERSION_RE.match(v)):
            raise ManifestError(f"{key} is not a version string")
        clean[key] = v
    dm = data.get("default_model")
    if dm is not None and (not isinstance(dm, str) or not re.match(r"^[A-Za-z0-9_.:/\-]{1,100}$", dm)):
        raise ManifestError("default_model is not a model id")
    clean["default_model"] = dm
    disabled = data.get("disabled_tools", [])
    if not isinstance(disabled, list) or not all(isinstance(d, str) and NAME_RE.match(d) for d in disabled):
        raise ManifestError("disabled_tools must be a list of tool names")
    clean["disabled_tools"] = sorted(set(disabled))
    notes = data.get("tool_notes", {})
    if not isinstance(notes, dict):
        raise ManifestError("tool_notes must be an object")
    clean["tool_notes"] = {
        k: v[:300] for k, v in notes.items()
        if isinstance(k, str) and NAME_RE.match(k) and isinstance(v, str)
    }
    tools = data.get("tools", [])
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ManifestError("tools must be a list of at most %d entries" % MAX_TOOLS)
    good: List[Dict[str, Any]] = []
    rejected: List[str] = []
    seen = set()
    for spec in tools:
        try:
            validate_spec(spec)
        except SpecError as e:
            rejected.append(str(e))
            continue
        if spec["name"] in seen:
            rejected.append(f"duplicate tool {spec['name']}")
            continue
        seen.add(spec["name"])
        good.append(spec)
    clean["tools"] = good
    return clean, rejected


def _version_tuple(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("+")[0])
    return tuple(int(n) for n in nums[:4])


def is_newer(candidate: Optional[str], current: str = __version__) -> bool:
    if not candidate or not VERSION_RE.match(candidate):
        return False
    return _version_tuple(candidate) > _version_tuple(current)


def merge(builtin: Dict[str, Any], server: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Overlay a validated server manifest on the built-in one."""
    out = copy.deepcopy(builtin)
    if not server:
        return out
    by_name = {t["name"]: t for t in out["tools"]}
    for t in server.get("tools", []):
        by_name[t["name"]] = t
    out["tools"] = list(by_name.values())
    out["disabled_tools"] = sorted(set(out.get("disabled_tools", [])) | set(server.get("disabled_tools", [])))
    out["tool_notes"] = {**out.get("tool_notes", {}), **server.get("tool_notes", {})}
    for key in ("manifest_version", "latest_version", "min_version", "default_model"):
        if server.get(key) is not None:
            out[key] = server[key]
    return out


def read_cache(path=None) -> Optional[Dict[str, Any]]:
    """Cached manifest record {fetched_at, etag, manifest} or None."""
    path = path or CACHE_FILE
    try:
        rec = json.loads(path.read_text())
        clean, _ = validate_manifest(rec["manifest"])
        return {"fetched_at": float(rec.get("fetched_at", 0)), "etag": rec.get("etag"), "manifest": clean}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_cache(manifest: Dict[str, Any], etag: Optional[str], path=None) -> None:
    path = path or CACHE_FILE
    try:
        ensure_config_dir()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "etag": etag, "manifest": manifest}))
        tmp.replace(path)
    except OSError:
        pass


def fetch(client: Optional[httpx.Client] = None, *, etag: Optional[str] = None,
          timeout: float = 5.0) -> Tuple[Optional[Dict[str, Any]], Optional[str], List[str]]:
    """GET the manifest. Returns (manifest or None if 304, etag, rejected notes)."""
    headers = {"User-Agent": f"free-code/{__version__}", "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        resp = client.get(manifest_url(), headers=headers, timeout=timeout)
        if resp.status_code == 304:
            return None, etag, []
        if resp.status_code != 200:
            raise ManifestError(f"HTTP {resp.status_code}")
        if len(resp.content) > MAX_MANIFEST_BYTES:
            raise ManifestError("manifest too large")
        clean, rejected = validate_manifest(resp.json())
        return clean, resp.headers.get("etag"), rejected
    finally:
        if own:
            client.close()


def refresh(config: Optional[Dict[str, Any]] = None, *, force: bool = False,
            client: Optional[httpx.Client] = None, path=None) -> Tuple[str, List[str]]:
    """Refresh the cache if stale. Returns (status text, rejected notes). Never raises."""
    path = path or CACHE_FILE
    if manifest_disabled(config):
        return "manifest fetch is off", []
    cached = read_cache(path)
    if cached and not force and time.time() - cached["fetched_at"] < CACHE_TTL_SECONDS:
        return "cache is fresh", []
    try:
        manifest, etag, rejected = fetch(client, etag=cached["etag"] if cached else None)
    except (httpx.HTTPError, ManifestError, ValueError) as e:
        return f"could not refresh ({type(e).__name__}: {e})", []
    if manifest is None and cached:
        write_cache(cached["manifest"], cached["etag"], path)
        return "not modified", []
    if manifest is None:
        return "empty response", []
    if cached and manifest["manifest_version"] < cached["manifest"]["manifest_version"]:
        return "ignored an older manifest (rollback protection)", rejected
    write_cache(manifest, etag, path)
    return f"updated to manifest v{manifest['manifest_version']}", rejected


def refresh_in_background(config: Optional[Dict[str, Any]] = None) -> Optional[threading.Thread]:
    """Refresh a stale cache without blocking startup. The result applies next run."""
    if manifest_disabled(config):
        return None
    cached = read_cache()
    if cached and time.time() - cached["fetched_at"] < CACHE_TTL_SECONDS:
        return None
    t = threading.Thread(target=refresh, args=(config,), daemon=True, name="free-code-manifest")
    t.start()
    return t


def load(config: Optional[Dict[str, Any]] = None, path=None) -> Dict[str, Any]:
    """The effective manifest: built-in overlaid with the cached server manifest."""
    path = path or CACHE_FILE
    if manifest_disabled(config):
        return copy.deepcopy(BUILTIN_MANIFEST)
    cached = read_cache(path)
    return merge(BUILTIN_MANIFEST, cached["manifest"] if cached else None)
