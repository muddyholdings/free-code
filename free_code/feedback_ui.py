"""Terminal prompts for the opt-in feedback flow (first-run ask, /feedback, thumbs)."""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console

from free_code import telemetry

console = Console()


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def first_run_consent(ask: Optional[Callable[[str], str]] = None) -> None:
    """Ask once, only in a real terminal. Declined or skipped means off."""
    if not telemetry.needs_consent() or not interactive():
        return
    console.print()
    console.print(telemetry.CONSENT_TEXT, highlight=False)
    try:
        telemetry.ask_consent(ask or (lambda p: console.input(p)))
    except (EOFError, KeyboardInterrupt):
        telemetry.set_enabled(False)
    state = telemetry.status()
    console.print(f"[dim]Feedback is {state}.[/dim]\n")


def _last_exchange(messages: List[Dict[str, str]]) -> str:
    user = next((m["content"] for m in reversed(messages)
                 if m["role"] == "user" and not m["content"].startswith(("[Tool result", "(Tool"))), "")
    asst = next((m["content"] for m in reversed(messages) if m["role"] == "assistant"), "")
    return f"USER:\n{user[:3500]}\n\nASSISTANT:\n{asst[:3500]}"


def feedback_command(text: str, messages: Optional[List[Dict[str, str]]] = None, model: str = "",
                     ask: Optional[Callable[[str], str]] = None) -> bool:
    """/feedback: send text the user typed. Works even when automatic reports are off,
    because the user is explicitly sending it. Returns True if sent."""
    ask = ask or (lambda p: console.input(p))
    text = (text or "").strip()
    if not text:
        text = ask("Your feedback (one line, Enter to cancel): ").strip()
    if not text:
        console.print("[dim]Cancelled.[/dim]")
        return False
    attachment = ""
    if messages:
        console.print("[dim]Nothing but your text is sent unless you attach the last exchange "
                      "(your last request and the reply, which may contain code).[/dim]")
        if ask("Attach the last exchange? [y/N] ").strip().lower() in ("y", "yes"):
            attachment = _last_exchange(messages)
    email = ask("Email for a reply (optional, Enter to skip): ").strip()
    ev = telemetry.build_feedback(text, email=email, attachment=attachment, model=model)
    ok = telemetry.submit(ev, force=True, wait=True)
    console.print("[green]Thanks, feedback sent.[/green]" if ok else
                  "[yellow]Could not send feedback (it is disabled by an environment variable).[/yellow]")
    return ok


def maybe_rate(task_number: int, model: str = "", ask: Optional[Callable[[str], str]] = None) -> None:
    """After a finished task: thumbs up/down (+ optional comment). Only when the
    user opted in, only in a terminal, after the 1st task and then every 5th."""
    if not telemetry.enabled() or not interactive():
        return
    if task_number != 1 and task_number % 5 != 0:
        return
    ask = ask or (lambda p: console.input(p))
    try:
        ans = ask("[dim]Rate that? u = up, d = down, Enter = skip[/dim] ").strip().lower()
        if ans not in ("u", "d", "up", "down"):
            return
        up = ans.startswith("u")
        comment = ""
        if not up:
            comment = ask("[dim]What went wrong? (optional, you type it, Enter to skip)[/dim] ").strip()
        telemetry.submit(telemetry.build_rating(up, comment, model))
        console.print("[dim]Thanks.[/dim]")
    except (EOFError, KeyboardInterrupt):
        return
