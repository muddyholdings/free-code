"""The tools one session offers: local tools, platform tools (built-in or from
the server manifest) and MCP tools, behind one lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from free_code import manifest as manifest_mod
from free_code.config import load_config
from free_code.mcp import MCPManager
from free_code.remote_tools import execute_spec
from free_code.tools import TOOL_DEFINITIONS, TOOL_REGISTRY

# Local tools that change files, run commands or commit: confirmed in safe mode.
WRITE_TOOLS = {"file_write", "apply_patch", "git_commit"}
SHELL_TOOLS = {"shell_command"}
# Local tools that send nothing off the machine and change nothing: never prompted.


class ToolSet:
    def __init__(self, project_root: Path, config: Optional[Dict[str, Any]] = None,
                 mcp: Optional[MCPManager] = None, manifest: Optional[Dict[str, Any]] = None) -> None:
        self.project_root = project_root
        self.config = config if config is not None else load_config()
        self.mcp = mcp
        self.manifest = manifest if manifest is not None else manifest_mod.load(self.config)
        self.disabled = set(self.manifest.get("disabled_tools", []))
        self.notes = self.manifest.get("tool_notes", {})
        self.remote: Dict[str, Dict[str, Any]] = {
            t["name"]: t for t in self.manifest.get("tools", []) if t["name"] not in self.disabled
        }
        self.local_defs = [d for d in TOOL_DEFINITIONS if d["name"] not in self.disabled]

    def names(self) -> List[str]:
        out = [d["name"] for d in self.local_defs] + list(self.remote)
        if self.mcp:
            out += list(self.mcp.routes)
        return out

    def definitions(self) -> List[Dict[str, Any]]:
        defs = list(self.local_defs)
        for spec in self.remote.values():
            defs.append({"name": spec["name"], "description": spec["description"], "parameters": spec["parameters"]})
        if self.mcp:
            defs.extend(self.mcp.definitions())
        return defs

    def kind(self, name: str) -> str:
        if name in self.disabled:
            return "disabled"
        if name in self.remote:
            return "platform"
        if self.mcp and name in self.mcp.routes:
            return "mcp"
        if name in TOOL_REGISTRY:
            return "local"
        return "unknown"

    def confirm_level(self, name: str) -> str:
        """none | write | shell | upload | mcp"""
        if name in self.remote:
            return self.remote[name].get("confirm", "none")
        if name in SHELL_TOOLS:
            return "shell"
        if name in WRITE_TOOLS:
            return "write"
        if self.mcp and name in self.mcp.routes:
            return "mcp"
        return "none"

    def prompt_section(self) -> str:
        lines: List[str] = []
        for d in self.definitions():
            props = d["parameters"].get("properties", {})
            req = set(d["parameters"].get("required", []))
            sig = ", ".join(f"{k}{'' if k in req else '?'}" for k in props)
            first = d["description"].split(". ")[0].rstrip(".")
            lines.append(f"- {d['name']}({sig}): {first}")
            if d["name"] in self.notes:
                lines.append(f"    note: {self.notes[d['name']]}")
        return "\n".join(lines)

    def execute(self, name: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """Run a tool. Returns (result text, source) where source is builtin|platform|mcp."""
        if name in self.disabled:
            return f"Error: tool {name} is switched off by the Free.ai manifest", "builtin"
        if name in self.remote:
            clean = {k: v for k, v in args.items() if not k.startswith("_")}
            return execute_spec(self.remote[name], clean, token=self.config.get("token"),
                                project_root=str(self.project_root)), "platform"
        if self.mcp and name in self.mcp.routes:
            text, is_err = self.mcp.call(name, args)
            return (text if not is_err or text.startswith("Error") else f"Error: {text}"), "mcp"
        func: Optional[Callable[..., Any]] = TOOL_REGISTRY.get(name)
        if func is None:
            return f"Error: Unknown tool: {name}", "builtin"
        call_args = dict(args)
        call_args["_project_root"] = str(self.project_root)
        return str(func(**call_args)), "builtin"
