"""Self-update: `free-code update` and a quiet once-a-day "newer version" notice.

The upgrade runs through whatever installed the CLI (uv tool, pipx, the
installer's private venv, plain pip, npm), so it never fights a package
manager. The daily check asks the manifest (which carries ``latest_version``),
falling back to PyPI, at most once per 24 hours, and only prints a line; it
never installs anything by itself. Opt out with ``FREE_CODE_NO_UPDATE_CHECK=1``
or ``free-code config set update_check false``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from free_code import __version__
from free_code import manifest as manifest_mod
from free_code.config import CONFIG_DIR, ensure_config_dir, load_config
from free_code.telemetry import install_method

PACKAGE = "freeai-code"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
STATE_FILE = CONFIG_DIR / "state.json"
CHECK_INTERVAL = 24 * 3600


def check_disabled(config: Optional[Dict[str, Any]] = None) -> bool:
    if os.environ.get("FREE_CODE_NO_UPDATE_CHECK") or os.environ.get("CI"):
        return True
    config = config if config is not None else load_config()
    return config.get("update_check") is False


def read_state(path=None) -> Dict[str, Any]:
    path = path or STATE_FILE
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: Dict[str, Any], path=None) -> None:
    path = path or STATE_FILE
    try:
        ensure_config_dir()
        path.write_text(json.dumps(state))
    except OSError:
        pass


def latest_from_pypi(client: Optional[httpx.Client] = None, timeout: float = 4.0) -> Optional[str]:
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        r = client.get(PYPI_URL, timeout=timeout, headers={"User-Agent": f"free-code/{__version__}"})
        if r.status_code != 200:
            return None
        v = r.json().get("info", {}).get("version")
        return v if isinstance(v, str) and manifest_mod.VERSION_RE.match(v) else None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            client.close()


def latest_version(config: Optional[Dict[str, Any]] = None, client: Optional[httpx.Client] = None) -> Optional[str]:
    """Latest release: the manifest's latest_version if present, else PyPI."""
    mf = manifest_mod.load(config)
    v = mf.get("latest_version")
    if v:
        return v
    return latest_from_pypi(client)


def _refresh_state(config, now, client, path) -> None:
    state = read_state(path)
    latest = latest_version(config, client) or state.get("latest")
    state.update({"checked_at": now, "latest": latest})
    write_state(state, path)


def daily_notice(config: Optional[Dict[str, Any]] = None, *, now: Optional[float] = None,
                 client: Optional[httpx.Client] = None, path=None, sync: bool = False) -> Optional[str]:
    """A one-line notice if a newer version is known. Never blocks the CLI.

    The notice comes from the last stored check. If that check is older than a
    day, a daemon thread refreshes it (manifest first, PyPI as fallback), so the
    answer shows up on the next run. ``sync=True`` refreshes inline (tests, `update --check`).
    """
    path = path or STATE_FILE
    config = config if config is not None else load_config()
    if check_disabled(config):
        return None
    now = time.time() if now is None else now
    state = read_state(path)
    if now - float(state.get("checked_at", 0)) >= CHECK_INTERVAL:
        if sync:
            _refresh_state(config, now, client, path)
            state = read_state(path)
        else:
            threading.Thread(target=_refresh_state, args=(config, now, client, path), daemon=True,
                             name="free-code-update-check").start()
    latest = state.get("latest")
    if manifest_mod.is_newer(latest):
        return f"free-code {latest} is available (you have {__version__}). Run: free-code update"
    minv = manifest_mod.load(config).get("min_version")
    if minv and manifest_mod.is_newer(minv):
        return f"This free-code ({__version__}) is older than the minimum supported {minv}. Run: free-code update"
    return None


def upgrade_command(method: Optional[str] = None, version: Optional[str] = None) -> List[str]:
    """The argv that upgrades this install. Raises RuntimeError when there is none."""
    method = method or install_method()
    spec = f"{PACKAGE}=={version}" if version else PACKAGE
    if method == "uv":
        if version:
            return ["uv", "tool", "install", "--force", spec]
        return ["uv", "tool", "upgrade", PACKAGE]
    if method == "pipx":
        if version:
            return ["pipx", "install", "--force", spec]
        return ["pipx", "upgrade", PACKAGE]
    # venv (installer's private venv), pip and npm (which pip-installs the CLI)
    return [sys.executable, "-m", "pip", "install", "--upgrade", spec]


def run_update(version: Optional[str] = None, *, dry_run: bool = False, method: Optional[str] = None) -> int:
    """Run the upgrade. Returns the exit code (0 on success)."""
    method = method or install_method()
    cmd = upgrade_command(method, version)
    if cmd[0] in ("uv", "pipx") and not shutil.which(cmd[0]):
        print(f"free-code was installed with {method} but {cmd[0]} is not on PATH.", file=sys.stderr)
        print("Re-run the installer instead: curl -fsSL https://free.ai/install.sh | sh", file=sys.stderr)
        return 1
    print(f"Updating free-code via {method}: {' '.join(cmd)}")
    if dry_run:
        return 0
    try:
        rc = subprocess.call(cmd)
    except OSError as e:
        print(f"Could not run the updater: {e}", file=sys.stderr)
        return 1
    if rc == 0:
        write_state({**read_state(), "checked_at": 0})
        print("Done. The new version starts next time you run free-code.")
    return rc
