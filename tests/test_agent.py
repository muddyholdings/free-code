import asyncio
import json
from pathlib import Path

import pytest

from free_code import agent as agent_mod
from free_code import sessions, telemetry
from free_code.agent import Agent


class Scripted:
    """Stands in for CoderClient: yields the next scripted reply each call."""
    model = "qwen7b"

    def __init__(self, replies):
        self.replies = list(replies)
        self.systems = []

    async def chat_stream(self, messages, system=None, tools=None):
        self.systems.append(system)
        yield {"type": "text", "content": self.replies.pop(0)}
        yield {"type": "done"}


def make(tmp_path, replies, **kw):
    a = Agent(tmp_path, {"safe_mode": True, "max_context_tokens": 2000}, output=kw.pop("output", "print"), **kw)
    a.client = Scripted(replies)
    return a


def call(tool, **args):
    return json.dumps({"tool": tool, "args": args})


def test_read_then_answer_and_session_saved(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    a = make(tmp_path, [call("file_read", path="a.txt"), "The file says hello."])
    asyncio.run(a.chat("what is in a.txt"))
    assert a.last_response == "The file says hello." and a.tool_log == [{"tool": "file_read", "ok": True}]
    assert a.client.systems[0] and a.client.systems[1]          # system prompt is sent on every request
    assert "generate_image" in a.client.systems[0] and "web_search" in a.client.systems[0]
    saved = sessions.load(a.session_id)
    assert saved and saved["messages"][0]["content"] == "what is in a.txt"


def test_write_needs_approval_in_noninteractive_mode(tmp_path):
    a = make(tmp_path, [call("file_write", path="x.txt", content="1"), "I could not write it."])
    asyncio.run(a.chat("write x"))
    assert not (tmp_path / "x.txt").exists() and a.denied_for_approval


def test_yes_flag_approves_writes(tmp_path):
    a = make(tmp_path, [call("file_write", path="x.txt", content="1"), "done"], auto_approve=True)
    asyncio.run(a.chat("write x"))
    assert (tmp_path / "x.txt").read_text() == "1" and not a.denied_for_approval


def test_yes_does_not_cover_dangerous_commands(tmp_path):
    (tmp_path / "keep.txt").write_text("k")
    a = make(tmp_path, [call("shell_command", command="rm -rf keep.txt"), "blocked"], auto_approve=True)
    asyncio.run(a.chat("clean"))
    assert (tmp_path / "keep.txt").exists()
    assert a.tool_log[0]["ok"] is False


def test_tool_failure_reports_telemetry_without_values(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "submit", lambda ev, **kw: seen.append(ev) or True)
    a = make(tmp_path, [call("file_read", path="/secret/path/nope.txt"), "ok"])
    asyncio.run(a.chat("read"))
    assert seen and seen[0]["kind"] == "tool_failure" and seen[0]["error_class"] == "not_found"
    assert "/secret" not in json.dumps(seen[0])


def test_unknown_tool_is_an_error_not_a_crash(tmp_path):
    a = make(tmp_path, [call("nope_tool"), "ok"])
    asyncio.run(a.chat("x"))
    assert a.tool_log == [{"tool": "nope_tool", "ok": False}]


def test_disabled_tool_is_refused(tmp_path):
    from free_code.toolset import ToolSet
    from free_code.builtin_manifest import BUILTIN_MANIFEST
    mf = dict(BUILTIN_MANIFEST, disabled_tools=["generate_image"])
    ts = ToolSet(tmp_path, {}, manifest=mf)
    a = make(tmp_path, [call("generate_image", prompt="x", output_path="a.png"), "ok"], toolset=ts)
    asyncio.run(a.chat("img"))
    assert a.tool_log[0]["ok"] is False and "generate_image" not in a.client.systems[0]


def test_platform_tool_runs_through_toolset(tmp_path, monkeypatch):
    from free_code import toolset
    monkeypatch.setattr(toolset, "execute_spec", lambda spec, args, **kw: "translated: hola")
    a = make(tmp_path, [call("translate_text", text="hello", target="es"), "Spanish is hola"])
    asyncio.run(a.chat("translate"))
    assert a.tool_log == [{"tool": "translate_text", "ok": True}] and a.last_response == "Spanish is hola"


def test_step_limit(tmp_path):
    a = make(tmp_path, [call("list_files")] * 5, max_steps=3)
    asyncio.run(a.chat("loop"))
    assert "stopped after 3 steps" in a.last_error


def test_resume_keeps_messages(tmp_path):
    a = make(tmp_path, ["first answer"])
    asyncio.run(a.chat("q1"))
    b = make(tmp_path, ["second answer"])
    b.load_session(sessions.load(a.session_id))
    asyncio.run(b.chat("q2"))
    assert [m["content"] for m in b.messages] == ["q1", "first answer", "q2", "second answer"]
    assert sessions.list_sessions(tmp_path.resolve())[0]["turns"] == 4


def test_mcp_tool_goes_through_manager(tmp_path):
    import sys
    from free_code.mcp import MCPManager
    from free_code.toolset import ToolSet
    m = MCPManager({"fake": {"command": sys.executable, "args": [str(Path(__file__).parent / "fake_mcp_server.py")]}})
    m.start()
    try:
        a = make(tmp_path, [call("mcp__fake__echo", text="yo"), "done"], auto_approve=True,
                 toolset=ToolSet(tmp_path, {}, mcp=m))
        asyncio.run(a.chat("use mcp"))
        assert a.tool_log == [{"tool": "mcp__fake__echo", "ok": True}]
        assert "mcp__fake__echo" in a.client.systems[0]
    finally:
        m.close()
