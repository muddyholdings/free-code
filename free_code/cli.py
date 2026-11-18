"""CLI entry point — Click commands for free-code."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from free_code import __version__
from free_code.config import (
    CONFIG_FILE,
    get_config_value,
    load_config,
    save_config,
    set_config_value,
)

console = Console()


def get_project_root() -> Path:
    """Determine the project root directory."""
    from free_code.tools.git_ops import git_root

    cwd = Path.cwd()
    root = git_root(str(cwd))
    if root:
        return Path(root)
    return cwd


def agent_options(f):
    """Options shared by every command that runs the agent."""
    f = click.option("--yes", "-y", "auto_yes", is_flag=True,
                     help="Approve file edits and commands without asking (dangerous commands are still blocked).")(f)
    f = click.option("--json", "as_json", is_flag=True, help="Print one JSON result and nothing else (for scripts and CI).")(f)
    f = click.option("--model", "model_override", default=None, help="Model id for this run (see: free-code models).")(f)
    f = click.option("--max-steps", type=int, default=None, help="Maximum tool steps (default 30).")(f)
    f = click.option("--no-mcp", is_flag=True, help="Do not start MCP servers.")(f)
    return f


def _make_agent(project_root: Path, *, auto_yes: bool = False, output: str = "rich",
                model_override: Optional[str] = None, max_steps: Optional[int] = None,
                no_mcp: bool = False, resume: Optional[str] = None, cont: bool = False):
    """Build an Agent with its MCP servers started (closed at exit)."""
    import atexit

    from free_code import sessions as sessions_mod
    from free_code.agent import MAX_AGENT_STEPS, Agent
    from free_code.mcp import MCPManager, load_servers
    from free_code.toolset import ToolSet

    config = load_config()
    if model_override:
        config["model"] = model_override
    mcp = None
    if not no_mcp:
        servers, notes = load_servers(project_root)
        if servers:
            mcp = MCPManager(servers)
            mcp.start()
            atexit.register(mcp.close)
            if output == "rich":
                for name, err in mcp.errors.items():
                    console.print(f"[yellow]MCP server {name} did not start: {err}[/yellow]")
        if output == "rich":
            for n in notes:
                console.print(f"[yellow]{n}[/yellow]")
    toolset = ToolSet(project_root, config, mcp=mcp)
    agent = Agent(project_root, config, toolset=toolset, auto_approve=auto_yes, output=output,
                  max_steps=max_steps or MAX_AGENT_STEPS)
    data = None
    if resume:
        data = sessions_mod.load(resume)
        if data is None:
            raise click.ClickException(f"No saved session matches {resume!r}. See: free-code sessions")
    elif cont:
        data = sessions_mod.latest(project_root)
    if data:
        agent.load_session(data)
    return agent


def _print_update_notice() -> None:
    from free_code import updater
    msg = updater.daily_notice()
    if msg:
        console.print(f"[dim]{msg}[/dim]")


def _startup_housekeeping() -> None:
    """Refresh the tool manifest and flush queued reports without blocking."""
    from free_code import manifest, telemetry
    manifest.refresh_in_background(load_config())
    if telemetry.enabled():
        import threading
        threading.Thread(target=telemetry.flush_queue, daemon=True).start()


def _run_noninteractive(agent, prompt: str, *, as_json: bool, print_mode: bool) -> None:
    """One task, no prompts. Exit 0 ok, 1 failed, 3 something needed approval."""
    import json as _json

    try:
        asyncio.run(agent.chat(prompt))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        from free_code import telemetry
        telemetry.report_exception(e, agent.client.model)
        if as_json:
            click.echo(_json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "session": agent.session_id}))
        else:
            click.echo(f"Agent error: {e}", err=True)
        sys.exit(1)
    ok = bool(agent.last_response) and not agent.last_error
    code = 0 if ok else (3 if agent.denied_for_approval else 1)
    if as_json:
        click.echo(_json.dumps({
            "ok": ok, "response": agent.last_response, "error": agent.last_error or None,
            "tools": agent.tool_log, "model": agent.client.model, "session": agent.session_id,
            "needs_approval": agent.denied_for_approval,
        }))
    elif print_mode:
        if agent.last_response:
            click.echo(agent.last_response)
        if agent.last_error:
            click.echo(agent.last_error, err=True)
    sys.exit(code)


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="free-code")
@click.option("-p", "--print", "print_prompt", default=None, metavar="PROMPT",
              help="Run one task non-interactively, print the answer and exit (scripts and CI).")
@click.option("--continue", "-c", "cont", is_flag=True, help="Resume the latest session for this project.")
@click.option("--resume", "resume_id", default=None, metavar="ID", help="Resume a saved session (see: free-code sessions).")
@agent_options
@click.pass_context
def main(ctx: click.Context, print_prompt: Optional[str], cont: bool, resume_id: Optional[str],
         auto_yes: bool, as_json: bool, model_override: Optional[str], max_steps: Optional[int],
         no_mcp: bool) -> None:
    """Free.ai Coder -- AI coding assistant for your terminal."""
    from free_code import telemetry
    telemetry.install_excepthook()
    if ctx.invoked_subcommand is not None:
        return
    if print_prompt is not None or as_json:
        prompt = print_prompt
        if prompt is None or prompt == "-":
            prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
        if not prompt.strip():
            raise click.UsageError("--json needs a prompt: use -p \"task\" or pipe one in.")
        agent = _make_agent(get_project_root(), auto_yes=auto_yes, output="json" if as_json else "print",
                            model_override=model_override, max_steps=max_steps, no_mcp=no_mcp,
                            resume=resume_id, cont=cont)
        _run_noninteractive(agent, prompt, as_json=as_json, print_mode=True)
        return
    ctx.invoke(chat, cont=cont, resume_id=resume_id, rc_on=False, auto_yes=auto_yes, model_override=model_override,
               max_steps=max_steps, no_mcp=no_mcp, as_json=False)


@main.command()
@click.option("--continue", "-c", "cont", is_flag=True, help="Resume the latest session for this project.")
@click.option("--resume", "resume_id", default=None, metavar="ID", help="Resume a saved session.")
@click.option("--rc", "rc_on", is_flag=True, help="Turn on Remote Control for this session (same as free-code rc).")
@agent_options
def chat(cont: bool = False, resume_id: Optional[str] = None, rc_on: bool = False, auto_yes: bool = False,
         as_json: bool = False, model_override: Optional[str] = None,
         max_steps: Optional[int] = None, no_mcp: bool = False) -> None:
    """Start an interactive coding session."""
    from free_code import feedback_ui
    from free_code.context.repo_map import generate_repo_map
    from free_code.models import get_model
    from free_code.ui.prompt import get_input, setup_history, show_help
    from free_code.ui.terminal import (
        print_error,
        print_model_info,
        print_success,
        print_welcome,
    )

    project_root = get_project_root()
    feedback_ui.first_run_consent()
    _startup_housekeeping()
    agent = _make_agent(project_root, auto_yes=auto_yes, model_override=model_override,
                        max_steps=max_steps, no_mcp=no_mcp, resume=resume_id, cont=cont)
    config = agent.config
    if rc_on:
        _start_rc(agent, project_root)

    print_welcome()
    print_model_info(config.get("provider", "free.ai"), get_model(config))
    console.print(f"  [dim]Project:[/dim] {project_root}")
    if agent.messages:
        console.print(f"  [dim]Resumed session {agent.session_id} ({len(agent.messages)} messages)[/dim]")
    extra = [n for n in agent.toolset.names() if n.startswith("mcp__")]
    if extra:
        console.print(f"  [dim]MCP tools:[/dim] {len(extra)}")
    console.print()
    _print_update_notice()

    setup_history()

    while True:
        try:
            user_input, from_app = _next_input(agent, get_input)
        except KeyboardInterrupt:
            console.print("\n[dim]Goodbye![/dim]")
            break

        if user_input is None:
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd = user_input.split()[0].lower()
            parts = user_input.split(maxsplit=1)
            rest = parts[1] if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit", "/q"):
                console.print("[dim]Goodbye![/dim]")
                break
            elif cmd == "/help":
                show_help()
                continue
            elif cmd == "/clear":
                agent.clear_history()
                print_success("Conversation cleared.")
                continue
            elif cmd == "/compact":
                agent.compact_history()
                print_success("Conversation compacted.")
                continue
            elif cmd == "/model":
                if rest:
                    _switch_model(agent, rest)
                else:
                    _pick_model(agent)
                config = agent.config
                continue
            elif cmd == "/models":
                _show_models(rest)
                continue
            elif cmd == "/tools":
                console.print(agent.toolset.prompt_section(), highlight=False)
                continue
            elif cmd == "/sessions":
                _show_sessions(project_root)
                continue
            elif cmd == "/resume":
                from free_code import sessions as sessions_mod
                data = sessions_mod.load(rest) if rest else sessions_mod.latest(project_root)
                if data:
                    agent.load_session(data)
                    print_success(f"Resumed {data['id']} ({len(agent.messages)} messages).")
                else:
                    print_error("No matching session. Try /sessions.")
                continue
            elif cmd == "/rc":
                if rest.strip() == "off":
                    _rc_off(agent)
                elif rest.strip() == "status":
                    console.print(f"Remote Control: {'on, ' + agent.rc.url if agent.rc and agent.rc.active else 'off'}")
                elif agent.rc and agent.rc.active:
                    console.print(f"Remote Control is already on: {agent.rc.url}")
                else:
                    _start_rc(agent, project_root)
                continue
            elif cmd == "/feedback":
                feedback_ui.feedback_command(rest, agent.messages, agent.client.model)
                continue
            elif cmd == "/update":
                from free_code import updater
                console.print(updater.daily_notice(sync=True) or "free-code is up to date.")
                continue
            elif cmd == "/config":
                _show_config()
                continue
            elif cmd == "/files":
                from free_code.tools.list_files import list_files
                result = list_files(_project_root=str(project_root), max_depth=2)
                console.print(f"[dim]{result}[/dim]")
                continue
            elif cmd == "/repo":
                repo_map = generate_repo_map(project_root)
                console.print(f"[dim]{repo_map}[/dim]")
                continue
            elif cmd == "/diff":
                from free_code.tools.git_ops import git_diff
                result = git_diff(_project_root=str(project_root))
                console.print(f"[dim]{result}[/dim]")
                continue
            elif cmd == "/status":
                from free_code.tools.git_ops import git_status
                result = git_status(_project_root=str(project_root))
                console.print(f"[dim]{result}[/dim]")
                continue
            elif cmd == "/test":
                from free_code.tools.test_runner import run_tests
                path = rest or None
                with console.status("Running tests..."):
                    result = run_tests(path=path, _project_root=str(project_root))
                console.print(result)
                continue
            else:
                print_error(f"Unknown command: {cmd}. Type /help for available commands.")
                continue

        try:
            agent._emit("prompt", {"text": user_input[:2000], "source": "app" if from_app else "terminal"})
            asyncio.run(agent.chat(user_input))
            if agent.last_error:
                _hint_login_on_limit(agent.last_error)
            feedback_ui.maybe_rate(agent.tasks_done, agent.client.model)
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")
        except Exception as e:
            from free_code import telemetry
            telemetry.report_exception(e, agent.client.model)
            print_error(f"Agent error: {e}")


def _next_input(agent, get_input):
    """Next line from the terminal, or a prompt sent from the app when Remote Control is on."""
    from free_code.rc import TerminalReader

    rc = agent.rc
    if agent.reader is None and not (rc and rc.active):
        return get_input("you> "), False
    reader = agent.reader or TerminalReader()
    agent.reader = reader
    if reader._busy:
        console.print("[bold cyan]you> [/bold cyan]", end="")
    reader.request("you> ")
    while True:
        if rc and rc.active:
            text = rc.next_remote_prompt()
            if text:
                console.print(f"\n[cyan][from the app][/cyan] {text}")
                return text, True
        got, line = reader.get(0.3 if (rc and rc.active) else None)
        if got:
            return (line.strip() if line is not None else None), False


def _start_rc(agent, project_root: Path) -> bool:
    import atexit

    from free_code.rc import RCError, RemoteControl, TerminalReader

    token = agent.config.get("token")
    if not token:
        console.print("[yellow]Remote Control needs a Free.ai sign-in. Run: free-code login[/yellow]")
        return False
    rc = RemoteControl(token, project=project_root.name, model=agent.client.model,
                       notify=lambda m: console.print(f"\n[dim]{m}[/dim]"))
    try:
        rc.start()
    except RCError as e:
        console.print(f"[red]{e}[/red]")
        return False
    agent.rc = rc
    agent.reader = agent.reader or TerminalReader()
    atexit.register(rc.stop)
    console.print(f"[green]Remote Control is on.[/green] Open the Free.ai app, or {rc.url}")
    console.print("[dim]Prompts and approvals also appear here; file writes and commands still need approval. "
                  "Only your account can see this session. End it with /rc off or free-code rc off.[/dim]")
    if agent.auto_approve or not agent.safe_mode:
        console.print("[yellow]Approvals are off in this session (--yes or safe_mode false), so a prompt sent "
                      "from the app can edit files and run commands without asking.[/yellow]")
    return True


def _rc_off(agent) -> None:
    if agent.rc and agent.rc.active:
        agent.rc.stop()
        console.print("[green]Remote Control ended.[/green]")
    else:
        console.print("[dim]Remote Control is not on in this session.[/dim]")


def _hint_login_on_limit(error: str) -> None:
    """When the anonymous limit or a missing key stops a request, offer the browser login."""
    if "free-code login" not in error or not sys.stdin.isatty():
        return
    try:
        ans = console.input("Sign in now in your browser? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans in ("", "y", "yes"):
        from free_code.auth import browser_login
        browser_login()


def _switch_model(agent, model_id: str) -> None:
    from free_code.client import CoderClient
    from free_code.models import describe, fetch_models, find_model
    from free_code.ui.terminal import print_success, print_warning

    set_config_value("model", model_id)
    agent.config = load_config()
    agent.client = CoderClient(agent.config)
    m = find_model(fetch_models(), model_id)
    if m:
        print_success(f"Model set to: {model_id} ({describe(m)})")
    else:
        print_warning(f"{model_id} is not in the Free.ai model list; using it anyway (fine for a BYOK provider).")


def _pick_model(agent) -> None:
    """Numbered picker over the chat and code models Free.ai serves."""
    from free_code.models import describe, fetch_models, filter_models, is_free, get_model
    from free_code.ui.terminal import print_info

    models = fetch_models()
    chat_models = filter_models(models, kind="code") + filter_models(models, kind="chat")
    free = [m for m in chat_models if is_free(m)]
    paid_count = len(chat_models) - len(free)
    print_info(f"Current model: {get_model(agent.config)}")
    for i, m in enumerate(free[:15], 1):
        console.print(f"  {i:2d}. {m['id']:28s} [green]free tier[/green]  {m.get('name', '')}")
    console.print(f"  [dim]{paid_count} paid models bill tokens and need a key: /models --paid | "
                  f"switch with /model <id>[/dim]")
    try:
        ans = console.input("Pick a number or paste a model id (Enter to keep): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not ans:
        return
    if ans.isdigit() and 1 <= int(ans) <= len(free[:15]):
        ans = free[int(ans) - 1]["id"]
    _switch_model(agent, ans)


def _show_models(args: str) -> None:
    from free_code.models import describe, fetch_models, filter_models, is_free

    kind = "chat"
    free_only = paid_only = False
    query = []
    for tok in args.split():
        if tok == "--free":
            free_only = True
        elif tok == "--paid":
            paid_only = True
        elif tok.startswith("--type="):
            kind = tok.split("=", 1)[1]
        else:
            query.append(tok)
    models = filter_models(fetch_models(), kind=kind, free_only=free_only, query=" ".join(query))
    if paid_only:
        models = [m for m in models if not is_free(m)]
    table = Table(title=f"Free.ai {kind} models ({len(models)})", show_header=True, header_style="bold")
    table.add_column("Id", style="cyan")
    table.add_column("Name")
    table.add_column("Tier")
    for m in models[:60]:
        table.add_row(m["id"], str(m.get("name", "")), describe(m))
    console.print(table)
    if len(models) > 60:
        console.print(f"[dim]{len(models) - 60} more: narrow with a search word, --free or --paid.[/dim]")


def _show_sessions(project_root: Path) -> None:
    import time as _time

    from free_code import sessions as sessions_mod

    items = sessions_mod.list_sessions(project_root)
    if not items:
        console.print("[dim]No saved sessions for this project yet.[/dim]")
        return
    table = Table(title="Sessions (this project)", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Updated")
    table.add_column("Msgs", justify="right")
    table.add_column("First request")
    for it in items[:20]:
        table.add_row(it["id"], _time.strftime("%Y-%m-%d %H:%M", _time.localtime(it["updated"] or 0)),
                      str(it["turns"]), it.get("title") or "")
    console.print(table)
    console.print("[dim]Resume: free-code --resume <id>   or   free-code --continue[/dim]")


@main.command()
@click.argument("question", nargs=-1, required=True)
@click.option("--print", "print_mode", is_flag=True, help="Plain answer on stdout, no formatting.")
@agent_options
def ask(question: tuple, print_mode: bool, auto_yes: bool, as_json: bool,
        model_override: Optional[str], max_steps: Optional[int], no_mcp: bool) -> None:
    """Ask a one-shot question about your codebase."""
    question_str = " ".join(question)
    output = "json" if as_json else "print" if print_mode else "rich"
    agent = _make_agent(get_project_root(), auto_yes=auto_yes, output=output, model_override=model_override,
                        max_steps=max_steps, no_mcp=no_mcp)
    if output != "rich":
        _run_noninteractive(agent, question_str, as_json=as_json, print_mode=print_mode)
        return
    from free_code.ui.terminal import print_error
    _startup_housekeeping()
    try:
        asyncio.run(agent.chat(question_str))
        if agent.last_error:
            _hint_login_on_limit(agent.last_error)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
    except Exception as e:
        from free_code import telemetry
        telemetry.report_exception(e, agent.client.model)
        print_error(str(e))
        sys.exit(1)


@main.command()
@click.argument("task", nargs=-1, required=True)
@click.option("--print", "print_mode", is_flag=True, help="Plain answer on stdout, no formatting.")
@agent_options
def run(task: tuple, print_mode: bool, auto_yes: bool, as_json: bool,
        model_override: Optional[str], max_steps: Optional[int], no_mcp: bool) -> None:
    """Execute a coding task (e.g., 'add unit tests for auth module')."""
    task_str = " ".join(task)
    project_root = get_project_root()
    output = "json" if as_json else "print" if print_mode else "rich"
    agent = _make_agent(project_root, auto_yes=auto_yes, output=output, model_override=model_override,
                        max_steps=max_steps, no_mcp=no_mcp)
    prompt = (f"Execute this task: {task_str}\n\n"
              "Work through it step by step. Read relevant files first, "
              "make the changes, then verify they work.")
    if output != "rich":
        _run_noninteractive(agent, prompt, as_json=as_json, print_mode=print_mode)
        return
    from free_code import feedback_ui
    from free_code.ui.terminal import print_error

    console.print(f"\n[bold]Task:[/bold] {task_str}")
    console.print(f"[dim]Project: {project_root}[/dim]\n")
    _startup_housekeeping()
    try:
        asyncio.run(agent.chat(prompt))
        if agent.last_error:
            _hint_login_on_limit(agent.last_error)
        feedback_ui.maybe_rate(1, agent.client.model)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
    except Exception as e:
        from free_code import telemetry
        telemetry.report_exception(e, agent.client.model)
        print_error(str(e))
        sys.exit(1)


@main.command()
@click.argument("prompt", nargs=-1, required=True)
@click.option("--repo", "repo_url", default=None, metavar="URL",
              help="Git repository URL to run the task against.")
@click.option("--model", "model",
              type=click.Choice(["qwen-coder-32b", "deepseek-coder"]),
              default="qwen-coder-32b", show_default=True,
              help="Cloud model to run the task with.")
@click.option("--no-watch", is_flag=True,
              help="Submit and exit without polling for the result.")
@click.option("--download", "download_dir", metavar="DIR",
              type=click.Path(file_okay=False), default=None,
              help="Download and unzip the task output into DIR on completion.")
def cloud(prompt: tuple, repo_url: Optional[str], model: str,
          no_watch: bool, download_dir: Optional[str]) -> None:
    """Run a coding task in the Free.ai cloud and stream its progress."""
    import time

    from free_code.cloud import (
        AuthRequired,
        CloudClient,
        CloudError,
        POLL_INTERVAL,
        queue_position_of,
        task_id_of,
    )
    from free_code.ui.terminal import print_error, print_info, print_markdown, print_success

    client = CloudClient()
    prompt_str = " ".join(prompt)

    try:
        task = client.submit(prompt_str, repo_url=repo_url, model=model)
    except AuthRequired as e:
        console.print(str(e))
        sys.exit(1)
    except CloudError as e:
        print_error(str(e))
        sys.exit(1)

    task_id = task_id_of(task)
    if not task_id:
        print_error(f"Unexpected API response: {task}")
        sys.exit(1)

    position = queue_position_of(task)
    if position is not None:
        print_success(f"Task submitted: {task_id} (queue position {position})")
    else:
        print_success(f"Task submitted: {task_id}")

    if no_watch and not download_dir:
        print_info("Check progress with: free-code tasks")
        return
    if no_watch and download_dir:
        print_info("--download requires watching the task; polling anyway.")

    last_printed = None
    try:
        while True:
            try:
                task = client.get(task_id)
            except CloudError as e:
                print_error(str(e))
                sys.exit(1)

            status = str(task.get("status", "")).lower()
            position = queue_position_of(task)
            line = f"queued (position {position})" if status == "queued" and position is not None else status
            if line != last_printed:
                print_info(f"Status: {line}")
                last_printed = line

            if status in ("completed", "failed", "cancelled", "error"):
                break
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        console.print(f"\n[dim]Stopped watching. Task {task_id} keeps running; "
                      f"check it with: free-code tasks[/dim]")
        return

    if status == "completed":
        result_text = task.get("result_text") or task.get("result") or ""
        if result_text:
            console.print()
            print_markdown(str(result_text))
        output_url = task.get("output_url")
        if output_url:
            print_info(f"Output: {output_url}")
            if download_dir:
                try:
                    dest = client.download_output(str(output_url), download_dir)
                    print_success(f"Output downloaded and extracted to {dest}")
                except CloudError as e:
                    print_error(str(e))
                    sys.exit(1)
        elif download_dir:
            print_info("Task completed but has no downloadable output.")
    else:
        error = task.get("error") or task.get("result_text") or "no details"
        print_error(f"Task {status}: {error}")
        sys.exit(1)


@main.command()
def tasks() -> None:
    """List your recent cloud tasks."""
    from free_code.cloud import AuthRequired, CloudClient, CloudError, task_id_of
    from free_code.ui.terminal import print_error

    client = CloudClient()
    try:
        items = client.list()
    except AuthRequired as e:
        console.print(str(e))
        sys.exit(1)
    except CloudError as e:
        print_error(str(e))
        sys.exit(1)

    if not items:
        console.print("[dim]No cloud tasks yet. Start one with: "
                      "free-code cloud \"task description\"[/dim]")
        return

    table = Table(title="Cloud Tasks", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Status")
    table.add_column("Created")
    table.add_column("Task")

    for item in items:
        task_id = task_id_of(item)
        preview = str(item.get("prompt") or item.get("preview") or "").replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:57] + "..."
        created = str(item.get("created_at") or item.get("created") or "")[:19].replace("T", " ")
        table.add_row(task_id[:8], str(item.get("status", "")), created, preview)

    console.print(table)


@main.command()
def init() -> None:
    """Initialize free-code in the current project."""
    from free_code.context.discovery import discover_files
    from free_code.context.repo_map import generate_repo_map
    from free_code.tools.git_ops import is_git_repo
    from free_code.tools.test_runner import detect_test_framework
    from free_code.ui.terminal import print_info, print_success, print_warning

    project_root = get_project_root()
    console.print(f"\n[bold]Initializing free-code[/bold] in {project_root}\n")

    # Check git
    if is_git_repo(str(project_root)):
        print_success("Git repository detected")
    else:
        print_warning("Not a git repository")

    # Discover files
    with console.status("Scanning files..."):
        files = discover_files(project_root)
    print_info(f"Found {len(files)} code files")

    # Detect test framework
    framework = detect_test_framework(str(project_root))
    if framework:
        print_info(f"Test framework: {framework}")
    else:
        print_info("No test framework detected")

    # Generate repo map
    with console.status("Generating repository map..."):
        repo_map = generate_repo_map(project_root)

    console.print(f"\n[dim]{repo_map}[/dim]")
    console.print(f"\n[success]Initialization complete. Run [bold]free-code[/bold] to start coding.[/success]\n")


@main.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """View or edit configuration."""
    if ctx.invoked_subcommand is None:
        _show_config()


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a configuration value."""
    from free_code.ui.terminal import print_success

    set_config_value(key, value)
    print_success(f"Set {key} = {value}")


@config.command("feedback")
@click.argument("setting", type=click.Choice(["on", "off", "status"]), default="status")
def config_feedback(setting: str) -> None:
    """Turn opt-in crash, tool-failure and rating reports on or off (default: off until you say yes)."""
    from free_code import telemetry

    if setting == "on":
        telemetry.set_enabled(True)
    elif setting == "off":
        telemetry.set_enabled(False)
    console.print(telemetry.status_text(), highlight=False)


@config.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Get a configuration value."""
    value = get_config_value(key)
    if value is not None:
        console.print(f"{key} = {value}")
    else:
        console.print(f"[dim]{key} is not set[/dim]")


@main.command()
@click.option("--manual", is_flag=True, help="Paste a key (or pick a BYOK provider) instead of using the browser.")
@click.option("--no-browser", is_flag=True, help="Print the sign-in address instead of opening a browser (SSH sessions).")
def login(manual: bool, no_browser: bool) -> None:
    """Sign in to Free.ai in your browser (or configure a BYOK provider with --manual)."""
    from free_code.auth import browser_login, login_flow

    if manual:
        login_flow()
        return
    if not browser_login(open_browser=not no_browser):
        sys.exit(1)


@main.command()
def logout() -> None:
    """Forget the saved Free.ai key on this machine."""
    from free_code.ui.terminal import print_success

    config = load_config()
    config["token"] = None
    save_config(config)
    print_success("Signed out. Delete the key at https://free.ai/account/#api-keys to revoke it everywhere.")


def _show_config() -> None:
    """Display current configuration as a table."""
    cfg = load_config()
    table = Table(title="Configuration", show_header=True, header_style="bold")
    table.add_column("Key", style="cyan")
    table.add_column("Value")

    for key, value in sorted(cfg.items()):
        display = str(value)
        # Mask sensitive values
        if key in ("token", "api_key") and value:
            display = display[:8] + "..." if len(display) > 8 else "***"
        table.add_row(key, display)

    table.add_row("[dim]config file[/dim]", f"[dim]{CONFIG_FILE}[/dim]")
    console.print(table)


@main.command()
@click.argument("text", nargs=-1)
def feedback(text: tuple) -> None:
    """Send feedback you type (works even with automatic reports off)."""
    from free_code import feedback_ui

    ok = feedback_ui.feedback_command(" ".join(text))
    if not ok:
        sys.exit(1)


@main.command()
@click.option("--free", "free_only", is_flag=True, help="Only models that cost no tokens (free tier).")
@click.option("--paid", "paid_only", is_flag=True, help="Only paid models (need a key with tokens).")
@click.option("--type", "kind", default="chat",
              help="chat, code, image, video, tts, stt, ocr, music, embeddings, translation ... (default chat)")
@click.option("--refresh", is_flag=True, help="Re-download the list instead of using the 6 hour cache.")
@click.argument("search", nargs=-1)
def models(free_only: bool, paid_only: bool, kind: str, refresh: bool, search: tuple) -> None:
    """List every model Free.ai serves, free tier first."""
    from free_code.models import describe, fetch_models, filter_models, is_free

    items = filter_models(fetch_models(force=refresh), kind=kind, free_only=free_only, query=" ".join(search))
    if paid_only:
        items = [m for m in items if not is_free(m)]
    table = Table(title=f"Free.ai {kind} models ({len(items)})", show_header=True, header_style="bold")
    table.add_column("Id", style="cyan")
    table.add_column("Name")
    table.add_column("Tier")
    for m in items[:80]:
        table.add_row(m["id"], str(m.get("name", "")), describe(m))
    console.print(table)
    if len(items) > 80:
        console.print(f"[dim]{len(items) - 80} more: add a search word, --free or --paid.[/dim]")
    console.print("[dim]Free tier = no token cost, within daily limits. Paid = billed in tokens, needs a key "
                  "with balance. Use one: free-code config set model <id>[/dim]")


@main.command()
@click.option("--check", "check_only", is_flag=True, help="Only say whether a newer version exists.")
@click.option("--version", "target", default=None, help="Install this exact version.")
@click.option("--dry-run", is_flag=True, help="Show the command without running it.")
def update(check_only: bool, target: Optional[str], dry_run: bool) -> None:
    """Upgrade free-code through whatever installed it (uv, pipx, pip, npm)."""
    from free_code import manifest, updater

    if check_only:
        manifest.refresh(load_config(), force=True)
        msg = updater.daily_notice(sync=True)
        console.print(msg or f"free-code {__version__} is up to date.")
        return
    sys.exit(updater.run_update(target, dry_run=dry_run))


@main.command()
@click.option("--refresh", is_flag=True, help="Fetch the server tool manifest now.")
def tools(refresh: bool) -> None:
    """Show the tools the agent has (built-in, platform, MCP) and the manifest status."""
    from free_code import manifest
    from free_code.mcp import load_servers
    from free_code.toolset import ToolSet

    cfg = load_config()
    if refresh:
        status, rejected = manifest.refresh(cfg, force=True)
        console.print(f"[dim]manifest: {status}[/dim]")
        for r in rejected:
            console.print(f"[yellow]rejected: {r}[/yellow]")
    ts = ToolSet(get_project_root(), cfg)
    table = Table(title="Tools", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Kind")
    table.add_column("Needs approval")
    for d in ts.definitions():
        n = d["name"]
        table.add_row(n, ts.kind(n), ts.confirm_level(n))
    console.print(table)
    mv = ts.manifest.get("manifest_version")
    off = ", ".join(ts.disabled) or "none"
    console.print(f"[dim]manifest v{mv}; switched off by server: {off}; "
                  f"MCP servers configured: {len(load_servers(get_project_root())[0])}[/dim]")


@main.command("sessions")
@click.option("--all", "all_projects", is_flag=True, help="Sessions from every project, not just this one.")
def sessions_cmd(all_projects: bool) -> None:
    """List saved sessions you can resume."""
    _show_sessions(None if all_projects else get_project_root())


@main.group()
def mcp() -> None:
    """Use MCP servers (for example sttai-mcp) as extra tools."""


@mcp.command("add")
@click.argument("name")
@click.argument("command", nargs=-1, required=True)
@click.option("--env", "envs", multiple=True, metavar="KEY=VALUE",
              help="Environment for the server. Use KEY='${NAME}' to read your own env var instead of storing a secret.")
@click.option("--url", "url", default=None, help="HTTP MCP server URL instead of a command.")
def mcp_add(name: str, command: tuple, envs: tuple, url: Optional[str]) -> None:
    """Add a stdio server: free-code mcp add stt-ai sttai-mcp --env STT_API_KEY='${STT_API_KEY}'"""
    from free_code.mcp import NAME_RE, USER_MCP_FILE, _load_file, save_user_servers

    if not NAME_RE.match(name):
        raise click.ClickException("name must be letters, digits, - or _ (max 40)")
    servers = _load_file(USER_MCP_FILE)
    cfg: dict = {}
    if url:
        cfg["url"] = url
    else:
        cfg["command"] = command[0]
        if len(command) > 1:
            cfg["args"] = list(command[1:])
    env = {}
    for e in envs:
        if "=" not in e:
            raise click.ClickException(f"--env needs KEY=VALUE, got {e!r}")
        k, v = e.split("=", 1)
        env[k] = v
    if env:
        cfg["env"] = env
    servers[name] = cfg
    save_user_servers(servers)
    console.print(f"[green]Added MCP server {name}.[/green] Check it: free-code mcp tools {name}")


@mcp.command("list")
def mcp_list() -> None:
    """List configured MCP servers."""
    from free_code.mcp import load_servers

    servers, notes = load_servers(get_project_root())
    if not servers:
        console.print("[dim]No MCP servers. Add one: free-code mcp add <name> <command> [args...][/dim]")
    for name, cfg in servers.items():
        what = cfg.get("url") or " ".join([cfg.get("command", "")] + [str(a) for a in cfg.get("args", [])])
        console.print(f"  [cyan]{name}[/cyan]  {what}")
    for n in notes:
        console.print(f"[yellow]{n}[/yellow]")


@mcp.command("remove")
@click.argument("name")
def mcp_remove(name: str) -> None:
    """Remove an MCP server from your user config."""
    from free_code.mcp import USER_MCP_FILE, _load_file, save_user_servers

    servers = _load_file(USER_MCP_FILE)
    if name not in servers:
        raise click.ClickException(f"No MCP server named {name}")
    del servers[name]
    save_user_servers(servers)
    console.print(f"Removed {name}.")


@mcp.command("tools")
@click.argument("name", required=False)
def mcp_tools(name: Optional[str]) -> None:
    """Connect to MCP servers and list their tools."""
    from free_code.mcp import MCPManager, load_servers

    servers, _ = load_servers(get_project_root())
    if name:
        servers = {k: v for k, v in servers.items() if k == name}
    if not servers:
        raise click.ClickException("No matching MCP server configured.")
    mgr = MCPManager(servers)
    try:
        mgr.start()
        for key, (server, real) in mgr.routes.items():
            tool = next(t for t in mgr.clients[server].tools if t["name"] == real)
            console.print(f"  [cyan]{key}[/cyan]  {' '.join(str(tool.get('description', '')).split())[:90]}")
        for server, err in mgr.errors.items():
            console.print(f"[red]{server}: {err}[/red]")
    finally:
        mgr.close()


@mcp.command("trust")
def mcp_trust() -> None:
    """Approve this project's .free-code/mcp.json (it can start programs, so it needs your OK)."""
    from free_code.mcp import _load_file, project_file, trust

    root = get_project_root()
    pf = project_file(root)
    if not pf.exists():
        raise click.ClickException(f"{pf} does not exist.")
    console.print(f"{pf} would start:")
    for name, cfg in _load_file(pf).items():
        console.print(f"  {name}: {cfg.get('url') or ' '.join([cfg.get('command', '')] + [str(a) for a in cfg.get('args', [])])}")
    if click.confirm("Allow these in this project?", default=False):
        trust(root)
        console.print("[green]Trusted.[/green]")


@main.group("rc", invoke_without_command=True)
@click.option("--continue", "-c", "cont", is_flag=True, help="Resume the latest session for this project.")
@click.option("--resume", "resume_id", default=None, metavar="ID", help="Resume a saved session.")
@click.option("--model", "model_override", default=None, help="Model id for this session.")
@click.pass_context
def rc(ctx: click.Context, cont: bool, resume_id: Optional[str], model_override: Optional[str]) -> None:
    """Remote Control: start a session you can watch and drive from the Free.ai app."""
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(chat, cont=cont, resume_id=resume_id, rc_on=True, auto_yes=False,
               as_json=False, model_override=model_override, max_steps=None, no_mcp=False)


@rc.command("off")
def rc_off() -> None:
    """End the Remote Control session started from this machine."""
    from free_code import rc as rc_mod

    token = load_config().get("token")
    if not token:
        raise click.ClickException("Not signed in. Run: free-code login")
    console.print(rc_mod.stop_from_state(token))


@rc.command("status")
def rc_status() -> None:
    """Show this account's Remote Control sessions."""
    import httpx

    from free_code import rc as rc_mod

    token = load_config().get("token")
    if not token:
        raise click.ClickException("Not signed in. Run: free-code login")
    try:
        r = httpx.get(f"{rc_mod.RELAY_BASE}/api/v1/code/rc/sessions/", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        rows = r.json().get("sessions", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        raise click.ClickException("Could not reach free.ai.")
    mine = rc_mod.read_state()
    if not rows:
        console.print("[dim]No remote sessions.[/dim]")
    for row in rows:
        mark = " (this machine)" if mine and mine.get("session_id") == row["id"] else ""
        console.print(f"  {row['id'][:8]}  {row['status']:5s}  {row['project']} on {row['host']}{mark}")
    console.print("[dim]Web: https://free.ai/code/sessions/[/dim]")
