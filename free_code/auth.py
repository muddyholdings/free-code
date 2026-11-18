"""Authentication flow for Free.ai and BYOK providers."""

from __future__ import annotations

import socket
import sys
import time
import webbrowser
from typing import Any, Callable, Dict, Optional

import httpx

from free_code import __version__

from rich.console import Console
from rich.prompt import Prompt

from free_code.config import load_config, save_config, set_config_value  # noqa: F401

console = Console()


def check_auth(config: Optional[dict] = None) -> bool:
    """Check if the user has valid authentication configured."""
    if config is None:
        config = load_config()
    provider = config.get("provider", "free.ai")
    if provider == "free.ai":
        # Free.ai allows anonymous usage with daily limits
        return True
    # BYOK providers need an API key
    return bool(config.get("api_key"))


def login_flow() -> None:
    """Interactive login flow."""
    config = load_config()
    console.print()
    console.print("[bold]Free.ai Coder: Login[/bold]")
    console.print()

    provider = Prompt.ask(
        "Provider",
        choices=["free.ai", "openai", "anthropic", "google", "openrouter"],
        default="free.ai",
    )

    if provider == "free.ai":
        console.print()
        console.print("Free.ai offers free daily limits with no account required.")
        console.print("For higher limits, get a free key at [link=https://free.ai/account/#api-keys]https://free.ai/account/#api-keys[/link]")
        console.print()
        token = Prompt.ask("Free.ai token (press Enter to skip)", default="")
        if token:
            set_config_value("token", token)
            console.print("[green]Token saved.[/green]")
        else:
            console.print("[dim]Using free anonymous tier.[/dim]")
        set_config_value("provider", "free.ai")
    else:
        console.print()
        key_name = "API key"
        if provider == "openrouter":
            console.print("Get an API key at [link=https://openrouter.ai/keys]https://openrouter.ai/keys[/link]")
        elif provider == "openai":
            console.print("Get an API key at [link=https://platform.openai.com/api-keys]https://platform.openai.com/api-keys[/link]")
        elif provider == "anthropic":
            console.print("Get an API key at [link=https://console.anthropic.com/settings/keys]https://console.anthropic.com/settings/keys[/link]")
        elif provider == "google":
            console.print("Get an API key at [link=https://aistudio.google.com/apikey]https://aistudio.google.com/apikey[/link]")

        api_key = Prompt.ask(f"\n{key_name}")
        if not api_key:
            console.print(f"[red]API key is required for {provider}.[/red]")
            sys.exit(1)

        set_config_value("provider", provider)
        set_config_value("api_key", api_key)
        console.print(f"[green]Configured {provider} provider.[/green]")

    console.print("[green]Login complete. Run [bold]free-code[/bold] to start coding.[/green]")


LOGIN_BASE = "https://free.ai"
MANUAL_URL = "https://free.ai/code/login/"
POLL_FLOOR = 2.0


class LoginFailed(RuntimeError):
    """The browser sign-in did not complete."""


def device_os() -> str:
    """A short OS name for the approve page, e.g. "Windows 11", "macOS", "Linux"."""
    import platform
    system = platform.system() or ""
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        rel = platform.release() or ""
        try:
            # Windows 11 still reports release "10"; build 22000+ is 11.
            if rel == "10" and int((platform.version() or "0").split(".")[-1]) >= 22000:
                rel = "11"
        except ValueError:
            pass
        return f"Windows {rel}".strip()
    return system[:19]


def start_login(client: httpx.Client, base: str = LOGIN_BASE) -> Dict[str, Any]:
    r = client.post(f"{base}/api/v1/code/login/start/",
                    json={"hostname": socket.gethostname()[:60], "client": f"free-code/{__version__}",
                          "os": device_os()},
                    headers={"User-Agent": f"free-code/{__version__}"}, timeout=15)
    if r.status_code == 429:
        raise LoginFailed("Too many sign-in attempts from this network. Try again in a few minutes.")
    if r.status_code != 201:
        raise LoginFailed(f"Could not start sign-in (HTTP {r.status_code}).")
    data = r.json()
    for key in ("code", "poll_token", "verification_url", "expires_in"):
        if key not in data:
            raise LoginFailed("Unexpected response from free.ai.")
    return data


def poll_login(client: httpx.Client, poll_token: str, expires_in: float, interval: float = 2.0,
               base: str = LOGIN_BASE, sleep: Optional[Callable[[float], None]] = None,
               clock: Optional[Callable[[], float]] = None) -> Dict[str, Any]:
    """Wait for the user to approve. Returns the approved response (has api_key)."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + float(expires_in)
    wait = max(float(interval), POLL_FLOOR)
    while clock() < deadline:
        sleep(wait)
        try:
            r = client.post(f"{base}/api/v1/code/login/poll/", json={"poll_token": poll_token},
                            headers={"User-Agent": f"free-code/{__version__}"}, timeout=15)
        except httpx.HTTPError:
            continue
        if r.status_code == 429:
            wait = min(wait + 1.0, 10.0)
            continue
        if r.status_code == 202:
            continue
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "approved" and data.get("api_key"):
                return data
            continue
        if r.status_code == 409:
            raise LoginFailed(str(r.json().get("error", "The account cannot take another key.")))
        if r.status_code == 410:
            raise LoginFailed("This code expired or was cancelled. Run free-code login again to get a new one.")
        raise LoginFailed(f"Unexpected response while waiting (HTTP {r.status_code}).")
    minutes = max(1, int(round(float(expires_in) / 60)))
    raise LoginFailed(f"The code expired after {minutes} minutes without approval. "
                      "Run free-code login again to get a new one.")


def browser_login(open_browser: bool = True, client: Optional[httpx.Client] = None) -> bool:
    """Sign in through the browser. Stores the key in the config (mode 600). True on success."""
    own = client is None
    client = client or httpx.Client()
    try:
        try:
            info = start_login(client)
        except httpx.HTTPError as e:
            console.print(f"[red]Could not reach free.ai ({type(e).__name__}).[/red] "
                          "You can still paste a key: free-code login --manual")
            return False
        except LoginFailed as e:
            console.print(f"[red]{e}[/red]")
            return False
        console.print()
        console.print("[bold]Sign in to Free.ai[/bold]")
        console.print(f"  Open:  [link={info['verification_url']}]{info['verification_url']}[/link]")
        console.print(f"  Code:  [bold]{info['code']}[/bold]  (check it matches on that page)")
        minutes = max(1, int(round(float(info["expires_in"]) / 60)))
        console.print(f"[dim]If the link does not open, go to {MANUAL_URL} on any device and type the code. "
                      f"New to Free.ai? Create a free account on the same page. The code lasts {minutes} minutes.[/dim]")
        if open_browser:
            try:
                webbrowser.open(info["verification_url"])
            except Exception:
                pass
        console.print("[dim]Waiting for approval...[/dim]")
        try:
            data = poll_login(client, info["poll_token"], info["expires_in"], info.get("interval", 2))
        except LoginFailed as e:
            console.print(f"[red]{e}[/red]")
            return False
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelled.[/dim]")
            return False
        set_config_value("provider", "free.ai")
        set_config_value("token", data["api_key"])
        who = f" as {data['email']}" if data.get("email") else ""
        console.print(f"[green]Signed in{who}.[/green] Key saved as [bold]{data.get('name', 'free-code')}[/bold].")
        return True
    finally:
        if own:
            client.close()
