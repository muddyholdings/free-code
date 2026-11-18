"""Remote Control: watch and drive this session from the Free.ai app or free.ai/code/sessions/.

Opt-in per session (`free-code rc`, or /rc inside a session). The CLI makes
OUTBOUND HTTPS calls only, authenticated with your key from `free-code login`
plus a per-session secret, to the relay at free.ai. The relay passes messages
between this terminal and the devices signed in to the SAME account. It keeps
only the newest 300 events, deleted an hour after the session ends, and it
executes nothing: tool calls that need approval still need it, here or from the
app, and every remote prompt is also printed in this terminal.

If the relay is unreachable the session simply keeps running locally; events
are buffered (newest 500) and the connection retries with backoff.
Protocol: docs/remote-control.md.
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from free_code import __version__
from free_code.config import CONFIG_DIR, ensure_config_dir

RELAY_BASE = "https://free.ai"
STATE_FILE = CONFIG_DIR / "rc.json"
PUSH_BATCH = 50
BUFFER_MAX = 500
HEARTBEAT = 10.0
MAX_BACKOFF = 30.0
PREVIEW_MAX = 800

_SECRET = re.compile(r"(sk-free-[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_\-]{20,}|Bearer\s+[A-Za-z0-9._\-]{16,}|"
                     r"(?i:api[_-]?key|token|secret|password)\s*[=:]\s*\S{8,})")


class RCError(RuntimeError):
    """Remote Control could not start."""


def redact(text: str, limit: int = PREVIEW_MAX) -> str:
    """Strip obvious credentials and cap the length before anything leaves the machine."""
    text = _SECRET.sub("[redacted]", str(text))
    return text if len(text) <= limit else text[:limit] + "..."


def args_preview(tool: str, args: Dict[str, Any]) -> str:
    clean = {k: v for k, v in (args or {}).items() if not str(k).startswith("_")}
    if tool == "shell_command" and "command" in clean:
        return redact(str(clean["command"]), 400)
    for key in ("path", "url", "query", "prompt", "text", "message"):
        if key in clean:
            extra = {k: v for k, v in clean.items() if k not in (key, "content", "old_string", "new_string")}
            return redact(f"{clean[key]}" + (f" {json.dumps(extra)[:200]}" if extra else ""), 400)
    return redact(json.dumps(clean, default=str), 400)


class TerminalReader:
    """One persistent background reader so the terminal and the relay can both
    answer. Only one input() is ever outstanding."""

    def __init__(self) -> None:
        self.q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._busy = False
        self._lock = threading.Lock()

    def request(self, prompt: str = "") -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._read, args=(prompt,), daemon=True, name="free-code-stdin").start()

    def _read(self, prompt: str) -> None:
        try:
            line: Optional[str] = input(prompt)
        except EOFError:
            line = None
        except Exception:
            line = None
        with self._lock:
            self._busy = False
        self.q.put(line)

    def get(self, timeout: Optional[float]) -> Tuple[bool, Optional[str]]:
        try:
            return True, self.q.get(timeout=timeout)
        except queue.Empty:
            return False, None


class RemoteControl:
    def __init__(self, token: str, project: str = "", model: str = "", base: str = RELAY_BASE,
                 client: Optional[httpx.Client] = None, host: Optional[str] = None,
                 notify: Optional[Callable[[str], None]] = None) -> None:
        self.token = token
        self.base = base.rstrip("/")
        self.project = project
        self.model = model
        self.host = host or socket.gethostname()
        self.client = client or httpx.Client(timeout=10)
        self.notify = notify or (lambda msg: None)
        self.session_id = ""
        self.secret = ""
        self.url = ""
        self.interval = 1.5
        self.active = False
        self.cancel = threading.Event()
        self.stopped = threading.Event()
        self.commands: "queue.Queue[str]" = queue.Queue()
        self.approvals: Dict[str, Dict[str, Any]] = {}
        self.connected = True
        self._out: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._after = 0
        self._thread: Optional[threading.Thread] = None
        self._last_push = 0.0
        self._failures = 0

    # ---- lifecycle
    def _headers(self, with_secret: bool = True) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.token}", "User-Agent": f"free-code/{__version__}"}
        if with_secret and self.secret:
            h["X-RC-Secret"] = self.secret
        return h

    def start(self) -> Dict[str, Any]:
        try:
            r = self.client.post(f"{self.base}/api/v1/code/rc/sessions/", headers=self._headers(False),
                                 json={"host": self.host, "project": self.project, "model": self.model})
        except httpx.HTTPError as e:
            raise RCError(f"Could not reach free.ai ({type(e).__name__}).")
        if r.status_code == 401:
            raise RCError("Not signed in. Run: free-code login")
        if r.status_code in (409, 429):
            raise RCError(str(r.json().get("error", "Too many remote sessions.")))
        if r.status_code != 201:
            raise RCError(f"Remote Control could not start (HTTP {r.status_code}).")
        data = r.json()
        self.session_id, self.secret = data["session_id"], data["secret"]
        self.url = data.get("url", "")
        self.interval = float(data.get("poll_interval", 1.5))
        self.active = True
        self._save_state()
        self.emit("status", {"state": "idle"})
        self._thread = threading.Thread(target=self._loop, daemon=True, name="free-code-rc")
        self._thread.start()
        return data

    def _save_state(self) -> None:
        try:
            ensure_config_dir()
            fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"session_id": self.session_id, "secret": self.secret, "pid": os.getpid()}, f)
        except OSError:
            pass

    def stop(self, end_remote: bool = True) -> None:
        if not self.active:
            return
        self.active = False
        self.stopped.set()
        if end_remote:
            try:
                self._flush()
                self.client.post(f"{self.base}/api/v1/code/rc/sessions/{self.session_id}/end/",
                                 headers=self._headers(), timeout=5)
            except httpx.HTTPError:
                pass
        try:
            STATE_FILE.unlink()
        except OSError:
            pass

    # ---- events out
    def emit(self, type_: str, data: Optional[Dict[str, Any]] = None) -> None:
        if not self.active:
            return
        with self._lock:
            self._out.append({"type": type_, "data": data or {}})
            if len(self._out) > BUFFER_MAX:
                del self._out[: len(self._out) - BUFFER_MAX]

    def _flush(self) -> bool:
        with self._lock:
            batch = self._out[:PUSH_BATCH]
        if not batch:
            return True
        r = self.client.post(f"{self.base}/api/v1/code/rc/sessions/{self.session_id}/events/",
                             headers=self._headers(), json={"events": batch})
        if r.status_code == 410:
            self._remote_ended()
            return True
        if r.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {r.status_code}")
        with self._lock:
            del self._out[: len(batch)]
        self._last_push = time.time()
        return True

    # ---- commands in
    def _poll(self) -> None:
        r = self.client.get(f"{self.base}/api/v1/code/rc/sessions/{self.session_id}/commands/",
                            headers=self._headers(), params={"after": self._after})
        if r.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {r.status_code}")
        data = r.json()
        for c in data.get("commands", []):
            self._after = max(self._after, int(c.get("seq", 0)))
            self._handle(c.get("type"), c.get("data") or {})
        self._after = max(self._after, int(data.get("last_seq", 0)))
        if data.get("status") == "ended":
            self._remote_ended()

    def _handle(self, typ: Optional[str], data: Dict[str, Any]) -> None:
        if typ == "prompt" and isinstance(data.get("text"), str):
            self.commands.put(data["text"])
        elif typ == "interrupt":
            self.cancel.set()
        elif typ in ("approve", "deny"):
            slot = self.approvals.get(str(data.get("approval_id")))
            if slot is not None and not slot["event"].is_set():
                slot["approved"] = typ == "approve"
                slot["by"] = "app"
                slot["event"].set()
        elif typ == "end":
            self._remote_ended()

    def _remote_ended(self) -> None:
        if self.active:
            self.notify("Remote Control was ended (from the app or free-code rc off).")
            self.active = False
            self.stopped.set()
            for slot in list(self.approvals.values()):
                slot["event"].set()
            try:
                STATE_FILE.unlink()
            except OSError:
                pass

    def _loop(self) -> None:
        backoff = self.interval
        while self.active:
            try:
                if self._out or time.time() - self._last_push >= HEARTBEAT:
                    self._flush()
                if self.active:
                    self._poll()
                if not self.connected:
                    self.connected = True
                    self.notify("Remote Control reconnected.")
                self._failures = 0
                backoff = self.interval
            except (httpx.HTTPError, ValueError, KeyError):
                self._failures += 1
                if self.connected and self._failures >= 3:
                    self.connected = False
                    self.notify("Remote Control lost the connection; the session continues locally and will retry.")
                backoff = min(MAX_BACKOFF, backoff * 1.7)
            self.stopped.wait(backoff)

    # ---- approvals
    def confirm(self, message: str, tool: str, preview: str, reader: Optional[TerminalReader],
                show: Callable[[str], None]) -> bool:
        """Ask in the terminal AND the app. First answer wins; default (Enter) approves
        like the terminal prompt does; a dropped relay leaves the terminal prompt working."""
        aid = uuid.uuid4().hex[:12]
        slot: Dict[str, Any] = {"event": threading.Event(), "approved": False, "by": ""}
        self.approvals[aid] = slot
        self.emit("approval_request", {"id": aid, "tool": tool, "preview": preview, "message": message})
        self.emit("status", {"state": "waiting_approval"})
        show(f"{message} [Y/n] (you can also answer from the app)")
        try:
            while not slot["event"].is_set():
                if reader is not None:
                    reader.request("")
                    got, line = reader.get(0.25)
                    if got:
                        ans = (line or "").strip().lower()
                        slot["approved"] = (ans in ("", "y", "yes")) if line is not None else False
                        slot["by"] = "terminal"
                        break
                else:
                    slot["event"].wait(0.25)
                if self.cancel.is_set():
                    slot["approved"], slot["by"] = False, "interrupt"
                    break
        finally:
            self.approvals.pop(aid, None)
        self.emit("approval_resolved", {"id": aid, "approved": bool(slot["approved"]), "by": slot["by"]})
        self.emit("status", {"state": "working"})
        return bool(slot["approved"])

    def next_remote_prompt(self) -> Optional[str]:
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None


def read_state() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def stop_from_state(token: str, base: str = RELAY_BASE, client: Optional[httpx.Client] = None) -> str:
    """`free-code rc off` from any terminal: end the recorded session. Returns a message."""
    st = read_state()
    if not st:
        return "No Remote Control session recorded on this machine."
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        r = client.post(f"{base.rstrip('/')}/api/v1/code/rc/sessions/{st['session_id']}/end/",
                        headers={"Authorization": f"Bearer {token}", "X-RC-Secret": st["secret"],
                                 "User-Agent": f"free-code/{__version__}"})
    except httpx.HTTPError as e:
        return f"Could not reach free.ai ({type(e).__name__}); the session stops when the terminal closes."
    finally:
        if own:
            client.close()
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
    return "Remote Control ended." if r.status_code == 200 else f"Relay answered HTTP {r.status_code}."
