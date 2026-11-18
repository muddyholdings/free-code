"""Opt-in feedback: crash reports, tool failures, ratings and typed feedback.

OFF until the user says yes (first-run prompt, or ``free-code config feedback on``).
``FREE_CODE_NO_TELEMETRY=1`` or ``DO_NOT_TRACK=1`` force it off. Nothing here ever
sends file contents, prompts, model replies, command lines or tool argument
values. What can leave the machine is exactly what the builders below produce:

  crash         CLI version, OS family, Python version, install method, the
                exception class, the CLI's own stack frames (module, function,
                line) and the exception message with quoted text, paths,
                numbers and long tokens replaced by placeholders.
  tool_failure  tool name, error class, model id, and the SHAPE of the arguments
                (names and type or length bucket, never values).
  rating        thumbs up or down, optionally a comment the user typed.
  feedback      text the user typed into /feedback. The last exchange is added
                only if the user answers yes to "attach it".

``FREE_CODE_TELEMETRY_DRYRUN=1`` prints each payload instead of sending it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from free_code import __version__
from free_code.config import CONFIG_DIR, ensure_config_dir, load_config, set_config_value

TELEMETRY_URL = "https://free.ai/api/v1/code/telemetry/"
QUEUE_FILE = CONFIG_DIR / "telemetry-queue.jsonl"
QUEUE_MAX = 50
SEND_TIMEOUT = 4.0
KINDS = ("crash", "tool_failure", "rating", "feedback")

_QUOTED = re.compile(r"""(['"])(?:(?!\1).){0,400}\1""", re.S)
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s'\"]+|(?:~|\.{1,2})?/[^\s'\"]+)")
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


def _env_off() -> bool:
    return bool(os.environ.get("FREE_CODE_NO_TELEMETRY") or os.environ.get("DO_NOT_TRACK"))


def status(config: Optional[Dict[str, Any]] = None) -> str:
    """'on', 'off' or 'unset' (not asked yet)."""
    if _env_off():
        return "off"
    config = config if config is not None else load_config()
    value = config.get("feedback")
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "unset"


def enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    return status(config) == "on"


def set_enabled(on: bool) -> None:
    set_config_value("feedback", "true" if on else "false")


# ---------------------------------------------------------------- sanitizing

def scrub_message(msg: str, limit: int = 160) -> str:
    """Make an exception message safe to send: no quoted text, paths, numbers or tokens."""
    msg = str(msg).splitlines()[0] if str(msg) else ""
    msg = _QUOTED.sub("<str>", msg)
    msg = _PATH.sub("<path>", msg)
    msg = _TOKEN.sub("<token>", msg)
    msg = _NUMBER.sub("N", msg)
    return msg[:limit]


def args_shape(args: Dict[str, Any]) -> Dict[str, str]:
    """Names and a type or size bucket for each argument. Never a value."""
    shape: Dict[str, str] = {}
    for k, v in (args or {}).items():
        if str(k).startswith("_"):
            continue
        key = str(k)[:40]
        if isinstance(v, bool):
            shape[key] = "bool"
        elif isinstance(v, int):
            shape[key] = "int"
        elif isinstance(v, float):
            shape[key] = "float"
        elif isinstance(v, str):
            n = len(v)
            shape[key] = "str:" + ("0" if n == 0 else "1-80" if n <= 80 else "81-1000" if n <= 1000 else "1000+")
        elif isinstance(v, (list, tuple)):
            shape[key] = f"list:{min(len(v), 99)}"
        elif isinstance(v, dict):
            shape[key] = f"dict:{min(len(v), 99)}"
        elif v is None:
            shape[key] = "null"
        else:
            shape[key] = type(v).__name__
    return shape


def classify_error(text: str) -> str:
    """Bucket a tool's 'Error: ...' result into a small fixed vocabulary."""
    t = (text or "").lower()
    table = (
        ("not found", "not_found"), ("no such file", "not_found"),
        ("permission", "permission"), ("timed out", "timeout"), ("timeout", "timeout"),
        ("blocked", "blocked"), ("missing required", "bad_args"), ("unknown tool", "unknown_tool"),
        ("api 429", "rate_limited"), ("limit", "rate_limited"),
        ("api 401", "auth"), ("api 402", "auth"), ("api 403", "auth"),
        ("api 5", "api_5xx"), ("api 4", "api_4xx"),
        ("could not reach", "network"), ("non-json", "bad_response"),
        ("not unique", "patch_no_match"), ("no match", "patch_no_match"), ("outside the project", "path_escape"),
    )
    for needle, label in table:
        if needle in t:
            return label
    return "other"


def install_method() -> str:
    exe = (sys.prefix or "").replace("\\", "/")
    if "/uv/tools/" in exe:
        return "uv"
    if "/pipx/venvs/" in exe:
        return "pipx"
    if exe.endswith("/.free-code/venv"):
        return "venv"
    if os.environ.get("FREE_CODE_INSTALLED_VIA") == "npm":
        return "npm"
    return "pip"


def _base() -> Dict[str, Any]:
    return {
        "origin": "cli",
        "version": __version__,
        "os": platform.system() or "unknown",
        "python": ".".join(str(x) for x in sys.version_info[:3]),
        "install": install_method(),
    }


def build_crash(exc: BaseException, model: str = "") -> Dict[str, Any]:
    frames: List[str] = []
    pkg_dir = Path(__file__).resolve().parent
    for fs in traceback.extract_tb(exc.__traceback__):
        try:
            inside = pkg_dir in Path(fs.filename).resolve().parents
        except OSError:
            inside = False
        if inside:
            frames.append(f"{Path(fs.filename).name}:{fs.lineno}:{fs.name}")
    ev = _base()
    ev.update({
        "kind": "crash",
        "error_class": type(exc).__name__,
        "message": scrub_message(str(exc)),
        "frames": frames[-12:],
        "model": model[:100],
    })
    return ev


def build_tool_failure(tool: str, error_text: str, args: Dict[str, Any], model: str = "", source: str = "builtin") -> Dict[str, Any]:
    ev = _base()
    ev.update({
        "kind": "tool_failure",
        "tool": tool[:60] if source != "mcp" else "mcp_tool",
        "tool_source": source,
        "error_class": classify_error(error_text),
        "model": model[:100],
        "args_shape": args_shape(args),
    })
    return ev


def build_rating(up: bool, comment: str = "", model: str = "") -> Dict[str, Any]:
    ev = _base()
    ev.update({"kind": "rating", "rating": 1 if up else -1, "message": comment.strip()[:1000], "model": model[:100]})
    return ev


def build_feedback(text: str, *, email: str = "", attachment: str = "", model: str = "") -> Dict[str, Any]:
    ev = _base()
    ev.update({"kind": "feedback", "message": text.strip()[:4000], "model": model[:100],
               "attached": bool(attachment), "attachment": attachment[:8000]})
    if email.strip():
        ev["email"] = email.strip()[:254]
    return ev


# ------------------------------------------------------------------ sending

def _post(event: Dict[str, Any], client: Optional[httpx.Client] = None) -> bool:
    own = client is None
    client = client or httpx.Client(timeout=SEND_TIMEOUT)
    try:
        r = client.post(TELEMETRY_URL, json=event, headers={"User-Agent": f"free-code/{__version__}"}, timeout=SEND_TIMEOUT)
        return r.status_code < 300
    except httpx.HTTPError:
        return False
    finally:
        if own:
            client.close()


def _queue(event: Dict[str, Any]) -> None:
    try:
        ensure_config_dir()
        lines = QUEUE_FILE.read_text().splitlines() if QUEUE_FILE.exists() else []
        lines.append(json.dumps(event))
        QUEUE_FILE.write_text("\n".join(lines[-QUEUE_MAX:]) + "\n")
    except OSError:
        pass


def flush_queue(client: Optional[httpx.Client] = None) -> int:
    """Retry queued events. Returns how many were delivered."""
    if not enabled() or not QUEUE_FILE.exists():
        return 0
    try:
        lines = [l for l in QUEUE_FILE.read_text().splitlines() if l.strip()]
    except OSError:
        return 0
    sent, keep = 0, []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if _post(ev, client):
            sent += 1
        else:
            keep.append(line)
    try:
        if keep:
            QUEUE_FILE.write_text("\n".join(keep) + "\n")
        else:
            QUEUE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return sent


def submit(event: Dict[str, Any], *, force: bool = False, wait: bool = False,
           client: Optional[httpx.Client] = None) -> bool:
    """Send an event if the user opted in (``force`` is for an explicit /feedback).

    Returns True if it was sent or queued. A dry run prints it and returns True.
    ``wait`` blocks up to the send timeout (crash path); otherwise a daemon
    thread sends it so the CLI never stalls.
    """
    if _env_off():
        return False
    if not force and not enabled():
        return False
    if event.get("kind") not in KINDS:
        return False
    if os.environ.get("FREE_CODE_TELEMETRY_DRYRUN"):
        print("[free-code feedback dry run] " + json.dumps(event, ensure_ascii=False), file=sys.stderr)
        return True

    def work() -> None:
        if not _post(event, client):
            _queue(event)

    if wait or client is not None:
        work()
    else:
        threading.Thread(target=work, daemon=True, name="free-code-telemetry").start()
    return True


def report_exception(exc: BaseException, model: str = "", wait: bool = True) -> None:
    """Report an unexpected exception (opt-in only). Never raises."""
    try:
        submit(build_crash(exc, model), wait=wait)
    except Exception:
        pass


def report_tool_failure(tool: str, result: str, args: Dict[str, Any], model: str = "", source: str = "builtin") -> None:
    try:
        submit(build_tool_failure(tool, result, args, model, source))
    except Exception:
        pass


def install_excepthook() -> None:
    """Send an uncaught crash (if opted in), then fall through to the normal hook."""
    previous = sys.excepthook

    def hook(etype, value, tb):  # pragma: no cover - exercised via report_exception
        if not issubclass(etype, (KeyboardInterrupt, SystemExit, BrokenPipeError)):
            value.__traceback__ = tb
            report_exception(value)
        previous(etype, value, tb)

    sys.excepthook = hook


# ----------------------------------------------------------------- first run

CONSENT_TEXT = (
    "Help improve free-code? You can send automatic, anonymous reports when something breaks:\n"
    "  - crashes: version, OS, Python, the CLI's own stack frames, error class\n"
    "  - tool failures: tool name, error class, model, argument names and sizes\n"
    "  - thumbs up/down and text you type into /feedback\n"
    "Never sent: your code, file contents, prompts, model replies, commands, paths or keys.\n"
    "Change it any time: free-code config feedback on|off   (details: free-code config feedback status)"
)


def needs_consent(config: Optional[Dict[str, Any]] = None) -> bool:
    return status(config) == "unset"


def ask_consent(ask) -> bool:
    """Ask once. ``ask`` is a callable(prompt)->str. Returns the answer (default no)."""
    answer = (ask("Send anonymous crash and tool-failure reports? [y/N] ") or "").strip().lower()
    on = answer in ("y", "yes")
    set_enabled(on)
    return on


def status_text(config: Optional[Dict[str, Any]] = None) -> str:
    st = status(config)
    lines = [f"feedback: {st}"]
    if _env_off():
        lines.append("(forced off by FREE_CODE_NO_TELEMETRY or DO_NOT_TRACK)")
    lines.append("")
    lines.append(CONSENT_TEXT.split("\n", 1)[1].split("\nChange it")[0])
    lines.append("Endpoint: " + TELEMETRY_URL)
    lines.append("Preview payloads without sending: FREE_CODE_TELEMETRY_DRYRUN=1")
    return "\n".join(lines)
