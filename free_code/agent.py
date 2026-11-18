"""Local agent loop — plan, execute, observe."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from free_code.client import CoderClient
from free_code.config import load_config
from free_code.context.repo_map import generate_repo_map
from free_code.context.window import build_context
from free_code import sessions as sessions_mod
from free_code import rc as rc_mod
from free_code import telemetry
from free_code.toolset import ToolSet
from free_code.tools.shell import is_dangerous
from free_code.ui.terminal import (
    confirm,
    print_error,
    print_markdown,
    print_tool_call,
    print_tool_result,
)

console = Console()

SYSTEM_PROMPT = """\
You are Free.ai Coder, an expert AI coding assistant running in the user's terminal.
You have access to their local filesystem and can read, write, and edit files.

## Tools Available
Names ending in ? are optional arguments.
{tools}

## Guidelines
- Read files before editing them to understand the full context
- Make minimal, focused changes
- Show diffs for significant edits
- Run tests after making changes when appropriate
- Use grep_search to find relevant code before making changes
- Prefer apply_patch over file_write for editing existing files
- Use the platform tools listed above (images, speech, transcription, OCR, web search,
  translation) when the task needs them; they call Free.ai and some cost tokens beyond
  the free tier
- Ask for confirmation before destructive operations
- Be concise in explanations, verbose in code

When you need to use a tool, respond with a JSON tool call in this format:
{{"tool": "tool_name", "args": {{"arg1": "value1"}}}}

After each tool result, analyze the output and decide the next step.
When you're done, provide a final summary of what was accomplished.
"""

MAX_AGENT_STEPS = 30


class Agent:
    """The coding agent — runs a plan/execute/observe loop."""

    def __init__(
        self,
        project_root: Path,
        config: Optional[Dict[str, Any]] = None,
        *,
        toolset: Optional[ToolSet] = None,
        auto_approve: bool = False,
        output: str = "rich",
        max_steps: int = MAX_AGENT_STEPS,
        session_id: Optional[str] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config = config or load_config()
        self.client = CoderClient(self.config)
        self.messages: List[Dict[str, str]] = []
        self.safe_mode = self.config.get("safe_mode", True)
        self.toolset = toolset or ToolSet(self.project_root, self.config)
        self.auto_approve = auto_approve
        self.output = output  # rich | print | json
        self.max_steps = max_steps
        self.session_id = session_id or sessions_mod.new_id()
        self.system: Optional[str] = None
        self.last_response = ""
        self.last_error = ""
        self.tool_log: List[Dict[str, Any]] = []
        self.denied_for_approval = False
        self.tasks_done = 0
        self.rc: Optional[rc_mod.RemoteControl] = None
        self.reader: Optional[rc_mod.TerminalReader] = None

    # ---- Remote Control (no-ops unless `free-code rc` is on for this session)
    def _emit(self, type_: str, data: Optional[Dict[str, Any]] = None) -> None:
        if self.rc is not None and self.rc.active:
            self.rc.emit(type_, data)

    def _cancelled(self) -> bool:
        if self.rc is not None and self.rc.cancel.is_set():
            self.rc.cancel.clear()
            self._emit("notice", {"text": "Interrupted from the app."})
            self.last_error = "interrupted"
            if self.output == "rich":
                console.print("[dim]Interrupted from the app.[/dim]")
            return True
        return False

    # ---- output helpers (rich in the terminal, quiet for --print / --json)
    def _say(self, text: str) -> None:
        self._emit("assistant", {"text": rc_mod.redact(text, 4000)})
        if self.output == "rich":
            print_markdown(text)

    def _err(self, text: str) -> None:
        if self.output == "json":
            return
        print_error(text)

    def _confirm(self, message: str, tool: str = "", preview: str = "") -> bool:
        if self.auto_approve:
            return True
        if self.rc is not None and self.rc.active and self.output == "rich":
            return self.rc.confirm(message, tool, preview, self.reader, lambda m: console.print(f"[warning]{m}[/warning]"))
        if self.output != "rich" or not sys.stdin.isatty():
            self.denied_for_approval = True
            return False
        return confirm(message)

    def load_session(self, data: Dict[str, Any]) -> None:
        self.session_id = data["id"]
        self.messages = list(data.get("messages", []))
        self.system = None  # rebuilt on the next message (tool list may have changed)

    def save_session(self) -> None:
        sessions_mod.save(self.session_id, self.project_root, self.client.model, self.messages)

    def _build_system(self, user_message: str) -> str:
        context, _ = build_context(
            self.project_root, user_message,
            max_tokens=self.config.get("max_context_tokens", 32000),
        )
        prompt = SYSTEM_PROMPT.format(tools=self.toolset.prompt_section())
        return prompt + f"\n\n## Project Context\n\n{context}"

    async def chat(self, user_message: str) -> None:
        """Process a user message through the agent loop."""
        self.last_error = ""
        self.last_response = ""
        if self.system is None:
            self.system = self._build_system(user_message)
        system = self.system

        self.messages.append({"role": "user", "content": user_message})
        self._emit("status", {"state": "working"})
        if self.rc is not None:
            self.rc.cancel.clear()

        for step in range(self.max_steps):
            if self._cancelled():
                break
            response_text = await self._get_response(system)
            if self._cancelled():
                break

            if not response_text:
                break

            tool_call = self._extract_tool_call(response_text)

            if tool_call:
                tool_name = str(tool_call["tool"])
                tool_args = tool_call.get("args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}

                pre_text = response_text[:response_text.find('{"tool"')].strip()
                if pre_text:
                    self._say(pre_text)

                result = await self._execute_tool(tool_name, tool_args)

                if result is None:
                    self.messages.append({"role": "assistant", "content": response_text})
                    self.messages.append({
                        "role": "user",
                        "content": "(Tool execution cancelled by user)"
                        if not self.denied_for_approval else
                        "(Tool execution was not approved. This is a non-interactive run: "
                        "do not retry; explain what you would have done.)",
                    })
                    continue

                self.messages.append({"role": "assistant", "content": response_text})
                self.messages.append({
                    "role": "user",
                    "content": f"[Tool result for {tool_name}]:\n{result}",
                })
            else:
                self._say(response_text)
                self.last_response = response_text
                self.messages.append({"role": "assistant", "content": response_text})
                break
        else:
            self.last_error = f"stopped after {self.max_steps} steps without a final answer"
            self._err(self.last_error)
        self.tasks_done += 1
        self.save_session()
        self._emit("status", {"state": "idle"})

    async def _get_response(self, system: Optional[str] = None) -> str:
        """Get a streaming response from the LLM."""
        chunks: List[str] = []
        spinner = Spinner("dots", text="Thinking...")

        live_console = console if self.output == "rich" else Console(quiet=True)
        with Live(spinner, console=live_console, refresh_per_second=10, transient=True):
            first_chunk = True
            async for event in self.client.chat_stream(self.messages, system=system):
                event_type = event.get("type", "")
                if self.rc is not None and self.rc.cancel.is_set():
                    return ""

                if event_type == "text":
                    content = event.get("content", "")
                    if first_chunk:
                        first_chunk = False
                    chunks.append(content)

                elif event_type == "error":
                    self.last_error = str(event.get("content", "Unknown error"))
                    self._err(self.last_error)
                    return ""

                elif event_type == "done":
                    break

        return "".join(chunks)

    def _extract_tool_call(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract a tool call JSON from the response text."""
        # Look for {"tool": "..."} pattern
        start = text.find('{"tool"')
        if start == -1:
            return None

        # Find the matching closing brace
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                        if "tool" in data:
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
        return None

    async def _execute_tool(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
    ) -> Optional[str]:
        """Execute a tool and return the result. Returns None if cancelled."""
        ts = self.toolset
        kind = ts.kind(tool_name)
        if kind in ("unknown", "disabled"):
            msg = (f"Error: Unknown tool: {tool_name}" if kind == "unknown"
                   else f"Error: tool {tool_name} is switched off by the Free.ai manifest")
            self._record(tool_name, tool_args, msg, "builtin")
            return msg

        level = ts.confirm_level(tool_name)
        shown = False

        def show() -> None:
            if self.output == "rich":
                print_tool_call(tool_name, tool_args)
            elif self.output == "print":
                print(f"> {tool_name}", file=sys.stderr)

        if self.safe_mode:
            if level == "write":
                show(); shown = True
                if not self._confirm("Apply this change?", tool_name, rc_mod.args_preview(tool_name, tool_args)):
                    return None
            elif level == "shell":
                cmd = tool_args.get("command", "")
                show(); shown = True
                if is_dangerous(cmd) and self.output == "rich":
                    console.print("[warning]This command could be dangerous.[/warning]")
                if not self._confirm(f"Run: {cmd}", tool_name, rc_mod.redact(str(cmd), 400)):
                    return None
            elif level in ("upload", "mcp"):
                show(); shown = True
                what = ("Upload this file to Free.ai?" if level == "upload"
                        else f"Run MCP tool {tool_name}?")
                if not self._confirm(what, tool_name, rc_mod.args_preview(tool_name, tool_args)):
                    return None
        else:
            if level == "shell" and is_dangerous(tool_args.get("command", "")):
                show(); shown = True
                if not self._confirm("This command could be dangerous. Run anyway?", tool_name, rc_mod.redact(str(tool_args.get("command", "")), 400)):
                    return None
        # An auto-approved run still never executes a dangerous command silently.
        if level == "shell" and self.auto_approve and is_dangerous(tool_args.get("command", "")) \
                and not self.config.get("allow_dangerous", False):
            msg = "Error: command blocked: it matches a dangerous pattern and --yes does not cover those"
            self._record(tool_name, tool_args, msg, "builtin")
            return msg

        if not shown:
            show()

        call_id = uuid.uuid4().hex[:12]
        self._emit("tool_call", {"id": call_id, "tool": tool_name if kind != "mcp" else tool_name,
                                 "preview": rc_mod.args_preview(tool_name, tool_args)})
        try:
            result, source = ts.execute(tool_name, dict(tool_args))
        except TypeError:
            clean = {k: v for k, v in tool_args.items() if not k.startswith("_")}
            try:
                result, source = ts.execute(tool_name, clean)
            except Exception as e2:
                result, source = f"Error executing {tool_name}: {e2}", "builtin"
        except Exception as e:
            result, source = f"Error executing {tool_name}: {e}", "builtin"

        self._record(tool_name, tool_args, result, source)
        self._emit("tool_result", {"id": call_id, "ok": not str(result).lstrip().startswith("Error"),
                                   "preview": rc_mod.redact(str(result), 800)})
        if self.output == "rich":
            print_tool_result(result, collapsed=len(str(result)) > 1000)
        return str(result)

    def _record(self, tool: str, args: Dict[str, Any], result: str, source: str) -> None:
        ok = not str(result).lstrip().startswith("Error")
        self.tool_log.append({"tool": tool, "ok": ok})
        if not ok:
            telemetry.report_tool_failure(tool, str(result), args, self.client.model,
                                          "mcp" if source == "mcp" else source)

    def clear_history(self) -> None:
        """Clear conversation history."""
        self.messages.clear()

    def compact_history(self) -> None:
        """Summarize conversation to save context."""
        if len(self.messages) <= 2:
            return
        # Keep first and last 4 messages, summarize the middle
        kept = self.messages[:1] + self.messages[-4:]
        summary = f"(Previous conversation with {len(self.messages)} messages was compacted)"
        self.messages = [{"role": "user", "content": summary}] + kept
