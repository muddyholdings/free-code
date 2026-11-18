"""Local sessions: every conversation is saved so it can be resumed.

Files live in ~/.free-code/sessions/<id>.json (mode 600) and stay on this
machine. They hold the messages, which may include file contents the model
read, so they are never uploaded by free-code. Only the newest 50 are kept.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from free_code.config import CONFIG_DIR

SESSIONS_DIR = CONFIG_DIR / "sessions"
KEEP = 50


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _title(messages: List[Dict[str, str]]) -> str:
    for m in messages:
        if m.get("role") == "user" and not m["content"].startswith(("[Tool result", "(Tool")):
            return " ".join(m["content"].split())[:80]
    return ""


def save(session_id: str, project_root: Path, model: str, messages: List[Dict[str, str]],
         directory=None) -> Optional[Path]:
    directory = directory or SESSIONS_DIR
    if not messages:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session_id}.json"
        prior: Dict[str, Any] = {}
        if path.exists():
            try:
                prior = json.loads(path.read_text())
            except ValueError:
                prior = {}
        data = {
            "id": session_id,
            "project": str(project_root),
            "model": model,
            "created": prior.get("created", time.time()),
            "updated": time.time(),
            "title": prior.get("title") or _title(messages),
            "messages": messages,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        _prune(directory)
        return path
    except OSError:
        return None


def _prune(directory: Path) -> None:
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_sessions(project_root: Optional[Path] = None, directory=None) -> List[Dict[str, Any]]:
    directory = directory or SESSIONS_DIR
    out: List[Dict[str, Any]] = []
    for p in directory.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if project_root is not None and d.get("project") != str(project_root):
            continue
        out.append({k: d.get(k) for k in ("id", "project", "model", "created", "updated", "title")}
                   | {"turns": len(d.get("messages", []))})
    return sorted(out, key=lambda d: d.get("updated") or 0, reverse=True)


def load(session_id: str, directory=None) -> Optional[Dict[str, Any]]:
    """Load by id or unique id prefix."""
    directory = directory or SESSIONS_DIR
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    exact = directory / f"{session_id}.json"
    try:
        if exact.exists():
            return json.loads(exact.read_text())
        matches = sorted(directory.glob(f"{session_id}*.json"))
        if len(matches) == 1:
            return json.loads(matches[0].read_text())
    except (OSError, ValueError):
        pass
    return None


def latest(project_root: Path, directory=None) -> Optional[Dict[str, Any]]:
    directory = directory or SESSIONS_DIR
    items = list_sessions(project_root, directory)
    return load(items[0]["id"], directory) if items else None
