"""translate_file: translate a JSON or YAML locale file with Free.ai translate.

Placeholders ({0}, {name}, {{name}}, %s, %(name)s, %d) are masked to positional
slots before sending and restored after. A string whose slots changed in the
translation is kept in the source language and reported, never silently
shipped with a broken slot.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

from free_code import __version__
from free_code.config import get_api_url, load_config

MAX_STRINGS = 400
SLOT_RE = re.compile(r"\{\{[^{}]+\}\}|\{[A-Za-z0-9_.]*\}|%\([A-Za-z0-9_]+\)[sdif]|%[sdif]|<[^<>]{1,40}>")
POS_RE = re.compile(r"\{(\d+)\}")


def mask(text: str) -> Tuple[str, List[str]]:
    slots: List[str] = []

    def sub(m: "re.Match[str]") -> str:
        slots.append(m.group(0))
        return "{%d}" % (len(slots) - 1)

    return SLOT_RE.sub(sub, text), slots


def unmask(text: str, slots: List[str]) -> Optional[str]:
    """Restore slots. None if the translation lost, added or duplicated one."""
    found = Counter(POS_RE.findall(text))
    if found != Counter(str(i) for i in range(len(slots))):
        return None
    return POS_RE.sub(lambda m: slots[int(m.group(1))], text)


def _walk(node: Any, path: Tuple = ()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, path + (i,))
    elif isinstance(node, str):
        yield path, node


def _set(root: Any, path: Tuple, value: str) -> None:
    for p in path[:-1]:
        root = root[p]
    root[path[-1]] = value


def default_output(src: Path, source: str, target: str) -> Path:
    """en.json -> es.json, app_en.json -> app_es.json, else app.json -> app.es.json."""
    stem = src.stem
    if source:
        if stem == source:
            return src.with_name(target + src.suffix)
        parts = re.split(r"([._\-])", stem)
        if source in parts:
            parts[parts.index(source)] = target
            return src.with_name("".join(parts) + src.suffix)
    return src.with_name(f"{stem}.{target}{src.suffix}")


def translate_file(path: str, target: str, source: str = "en", output_path: Optional[str] = None,
                   _project_root: Optional[str] = None, _client: Optional[httpx.Client] = None) -> str:
    root = Path(_project_root or ".").resolve()
    src = Path(path)
    src = (src if src.is_absolute() else root / src).resolve()
    if not src.is_file():
        return f"Error: file not found: {path}"
    suffix = src.suffix.lower()
    if suffix not in (".json", ".yaml", ".yml"):
        return "Error: translate_file supports .json, .yaml and .yml locale files"
    try:
        data = json.loads(src.read_text("utf-8")) if suffix == ".json" else yaml.safe_load(src.read_text("utf-8"))
    except (ValueError, yaml.YAMLError, OSError) as e:
        return f"Error: could not parse {path} ({type(e).__name__})"
    strings = list(_walk(data))
    if not strings:
        return "Error: no translatable strings found"
    if len(strings) > MAX_STRINGS:
        return f"Error: {len(strings)} strings is over the {MAX_STRINGS} per-call cap; split the file"
    dest = Path(output_path) if output_path else default_output(src, source, target)
    dest = (dest if dest.is_absolute() else root / dest).resolve()
    if root != dest and root not in dest.parents:
        return "Error: refusing to write outside the project"

    cfg = load_config()
    url = "https://api.free.ai/v1/translate/"
    headers = {"User-Agent": f"free-code/{__version__}"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    own = _client is None
    client = _client or httpx.Client(timeout=60)
    translated = 0
    kept: List[str] = []
    try:
        for p, text in strings:
            if not text.strip():
                continue
            masked, slots = mask(text)
            body: Dict[str, Any] = {"text": masked, "target": target}
            if source:
                body["source"] = source
            try:
                r = client.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                return f"Error: could not reach Free.ai ({type(e).__name__}) after {translated} strings; nothing written"
            if r.status_code in (401, 402, 403, 429):
                return (f"Error: API {r.status_code} after {translated} strings; nothing written. "
                        "Get a free key at https://free.ai/account/#api-keys then run: free-code login")
            if r.status_code >= 400:
                kept.append(".".join(map(str, p)))
                continue
            out = (r.json() or {}).get("translated_text") or (r.json() or {}).get("text")
            restored = unmask(out, slots) if isinstance(out, str) and out.strip() else None
            if restored is None:
                kept.append(".".join(map(str, p)))
                continue
            _set(data, p, restored)
            translated += 1
    finally:
        if own:
            client.close()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".json":
        dest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    else:
        dest.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
    msg = f"Translated {translated} of {len(strings)} strings into {target}. Wrote {dest.relative_to(root)}."
    if kept:
        msg += f" Kept in {source or 'the source language'} (slot check failed or API error): {', '.join(kept[:20])}"
        if len(kept) > 20:
            msg += f" and {len(kept) - 20} more"
    return msg
