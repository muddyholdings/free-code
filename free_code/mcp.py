"""A small MCP (Model Context Protocol) client so free-code can use any MCP server.

Transports: stdio (a local command, newline-delimited JSON-RPC, what sttai-mcp,
Claude Desktop and Cursor use) and streamable HTTP (a URL that answers JSON or
an SSE stream). Config uses the same ``mcpServers`` shape as Claude Desktop:

    {"mcpServers": {"stt-ai": {"command": "sttai-mcp", "env": {"STT_API_KEY": "${STT_API_KEY}"}}}}

User servers live in ``~/.free-code/mcp.json``. A project may ship
``.free-code/mcp.json``, but a repository must not be able to start programs
on your machine just by being opened, so a project file only loads after you
approve its exact contents (``free-code mcp trust``). ``${VAR}`` in env and
header values is read from your environment, so keys need not be stored.
Tools appear to the model as ``mcp__<server>__<tool>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from free_code import __version__
from free_code.config import CONFIG_DIR, ensure_config_dir

PROTOCOL_VERSION = "2024-11-05"
USER_MCP_FILE = CONFIG_DIR / "mcp.json"
TRUST_FILE = CONFIG_DIR / "mcp-trusted.json"
CALL_TIMEOUT = 120.0
INIT_TIMEOUT = 30.0
MAX_RESULT_CHARS = 20000
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPError(RuntimeError):
    """An MCP server failed or answered with an error."""


def expand_env(value: str) -> str:
    return ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _load_file(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers", data.get("servers", {})) if isinstance(data, dict) else {}
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if isinstance(name, str) and NAME_RE.match(name) and isinstance(cfg, dict) \
                    and (cfg.get("command") or cfg.get("url")):
                out[name] = cfg
    return out


def project_file(project_root: Path) -> Path:
    return project_root / ".free-code" / "mcp.json"


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def is_trusted(project_root: Path, trust_path=None) -> bool:
    trust_path = trust_path or TRUST_FILE
    pf = project_file(project_root)
    if not pf.exists():
        return False
    try:
        trusted = json.loads(trust_path.read_text())
    except (OSError, ValueError):
        return False
    return trusted.get(str(project_root.resolve())) == _digest(pf)


def trust(project_root: Path, trust_path=None) -> None:
    trust_path = trust_path or TRUST_FILE
    pf = project_file(project_root)
    try:
        trusted = json.loads(trust_path.read_text())
    except (OSError, ValueError):
        trusted = {}
    trusted[str(project_root.resolve())] = _digest(pf)
    ensure_config_dir()
    trust_path.write_text(json.dumps(trusted))


def load_servers(project_root: Optional[Path] = None, user_path=None,
                 trust_path=None) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Return (servers, notes). A project file is skipped unless trusted."""
    user_path = user_path or USER_MCP_FILE
    trust_path = trust_path or TRUST_FILE
    servers = _load_file(user_path)
    notes: List[str] = []
    if project_root is not None and project_file(project_root).exists():
        if is_trusted(project_root, trust_path):
            servers.update(_load_file(project_file(project_root)))
        else:
            notes.append(f"{project_file(project_root)} was ignored: run `free-code mcp trust` to approve it.")
    return servers, notes


def save_user_servers(servers: Dict[str, Dict[str, Any]], user_path=None) -> None:
    user_path = user_path or USER_MCP_FILE
    ensure_config_dir()
    user_path.write_text(json.dumps({"mcpServers": servers}, indent=2))
    try:
        os.chmod(user_path, 0o600)
    except OSError:
        pass


class _Stdio:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        cmd = [expand_env(str(cfg["command"]))] + [expand_env(str(a)) for a in cfg.get("args", [])]
        env = dict(os.environ)
        for k, v in (cfg.get("env") or {}).items():
            env[str(k)] = expand_env(str(v))
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, text=True, bufsize=1, cwd=cfg.get("cwd") or None)
        except OSError as e:
            raise MCPError(f"could not start {cmd[0]}: {e}")
        self.q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except ValueError:
                continue
        self.q.put(None)

    def send(self, msg: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise MCPError("server closed its input")

    def request(self, msg: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self.send(msg)
        while True:
            try:
                item = self.q.get(timeout=timeout)
            except queue.Empty:
                raise MCPError(f"timed out waiting for {msg.get('method')}")
            if item is None:
                raise MCPError("server exited")
            if item.get("id") == msg["id"]:
                return item
            # Requests from the server (sampling, roots) are not supported: answer politely.
            if "method" in item and "id" in item:
                self.send({"jsonrpc": "2.0", "id": item["id"],
                           "error": {"code": -32601, "message": "not supported by free-code"}})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass


class _Http:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.url = expand_env(str(cfg["url"]))
        if not self.url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise MCPError("HTTP MCP servers must use https (or localhost)")
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                        "User-Agent": f"free-code/{__version__}"}
        for k, v in (cfg.get("headers") or {}).items():
            self.headers[str(k)] = expand_env(str(v))
        self.client = httpx.Client(timeout=CALL_TIMEOUT)
        self.session: Optional[str] = None

    def send(self, msg: Dict[str, Any]) -> None:
        self._post(msg)

    def _post(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        r = self.client.post(self.url, json=msg, headers=headers)
        if r.headers.get("mcp-session-id"):
            self.session = r.headers["mcp-session-id"]
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code} from MCP server")
        if "id" not in msg:
            return None
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        item = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if item.get("id") == msg["id"]:
                        return item
            raise MCPError("no response in event stream")
        return r.json()

    def request(self, msg: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        try:
            out = self._post(msg)
        except httpx.HTTPError as e:
            raise MCPError(f"MCP HTTP error: {type(e).__name__}")
        if out is None:
            raise MCPError("empty response")
        return out

    def close(self) -> None:
        self.client.close()


class MCPClient:
    """One connected MCP server."""

    def __init__(self, name: str, cfg: Dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self._id = 0
        self.transport: Any = None
        self.tools: List[Dict[str, Any]] = []

    def _next(self) -> int:
        self._id += 1
        return self._id

    def connect(self) -> None:
        self.transport = _Http(self.cfg) if self.cfg.get("url") else _Stdio(self.cfg)
        try:
            resp = self.transport.request({
                "jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                           "clientInfo": {"name": "free-code", "version": __version__}}}, INIT_TIMEOUT)
            if "error" in resp:
                raise MCPError(str(resp["error"].get("message", "initialize failed")))
            self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.tools = self._list_tools()
        except Exception:
            self.close()
            raise

    def _list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            resp = self.transport.request({"jsonrpc": "2.0", "id": self._next(), "method": "tools/list",
                                           "params": params}, INIT_TIMEOUT)
            if "error" in resp:
                raise MCPError(str(resp["error"].get("message", "tools/list failed")))
            result = resp.get("result", {})
            tools.extend(t for t in result.get("tools", []) if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call(self, tool: str, arguments: Dict[str, Any]) -> Tuple[str, bool]:
        """Run a tool. Returns (text, is_error)."""
        resp = self.transport.request({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                                       "params": {"name": tool, "arguments": arguments}}, CALL_TIMEOUT)
        if "error" in resp:
            return f"Error: {resp['error'].get('message', 'MCP error')}", True
        result = resp.get("result", {})
        parts: List[str] = []
        for block in result.get("content", []) or []:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") in ("image", "audio"):
                    parts.append(f"[{block['type']} content omitted]")
                elif block.get("type") == "resource":
                    res = block.get("resource", {})
                    parts.append(str(res.get("text") or res.get("uri") or "[resource]"))
        text = "\n".join(parts) or json.dumps(result.get("structuredContent", result))[:MAX_RESULT_CHARS]
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n... (truncated)"
        return text, bool(result.get("isError"))

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None


def tool_key(server: str, tool: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", tool)
    return f"mcp__{server.replace('-', '_')}__{safe}"


class MCPManager:
    """Connects every configured server and exposes their tools to the agent."""

    def __init__(self, servers: Dict[str, Dict[str, Any]]) -> None:
        self.servers = servers
        self.clients: Dict[str, MCPClient] = {}
        self.errors: Dict[str, str] = {}
        self.routes: Dict[str, Tuple[str, str]] = {}  # tool key -> (server, real tool name)

    def start(self) -> None:
        for name, cfg in self.servers.items():
            client = MCPClient(name, cfg)
            try:
                client.connect()
            except (MCPError, httpx.HTTPError, OSError) as e:
                self.errors[name] = str(e)[:200]
                continue
            self.clients[name] = client
            for t in client.tools:
                self.routes[tool_key(name, t["name"])] = (name, t["name"])

    def definitions(self) -> List[Dict[str, Any]]:
        defs: List[Dict[str, Any]] = []
        for key, (server, real) in self.routes.items():
            tool = next(t for t in self.clients[server].tools if t["name"] == real)
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
            defs.append({
                "name": key,
                "description": f"[MCP {server}] {' '.join(str(tool.get('description', real)).split())}"[:600],
                "parameters": schema,
            })
        return defs

    def call(self, key: str, arguments: Dict[str, Any]) -> Tuple[str, bool]:
        if key not in self.routes:
            return f"Error: unknown MCP tool {key}", True
        server, real = self.routes[key]
        try:
            return self.clients[server].call(real, {k: v for k, v in arguments.items() if not k.startswith("_")})
        except (MCPError, httpx.HTTPError) as e:
            return f"Error: MCP server {server} failed: {e}", True

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
        self.clients.clear()
