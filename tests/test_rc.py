import asyncio
import json
import threading
import time

import httpx
import pytest

from free_code import rc as rcmod
from free_code.agent import Agent


class Relay:
    """A fake relay with the real server's contract: secret on CLI calls, cursors, 410 when ended."""

    def __init__(self):
        self.events, self.commands, self.seq = [], [], 0
        self.status = "live"
        self.fail = False
        self.seen_headers = []

    def handler(self, req):
        if self.fail:
            raise httpx.ConnectError("down")
        self.seen_headers.append(dict(req.headers))
        path = req.url.path
        if path == "/api/v1/code/rc/sessions/" and req.method == "POST":
            return httpx.Response(201, json={"session_id": "11111111-1111-1111-1111-111111111111", "secret": "s3cret",
                                             "poll_interval": 0.01, "url": "https://free.ai/code/sessions/x/"})
        assert req.headers.get("x-rc-secret") == "s3cret", "CLI call without the secret"
        if path.endswith("/events/"):
            if self.status != "live":
                return httpx.Response(410, json={})
            for e in json.loads(req.content)["events"]:
                self.seq += 1
                self.events.append(dict(e, seq=self.seq))
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/commands/"):
            after = int(req.url.params.get("after", 0))
            cmds = [c for c in self.commands if c["seq"] > after]
            return httpx.Response(200, json={"commands": cmds, "last_seq": cmds[-1]["seq"] if cmds else after, "status": self.status})
        if path.endswith("/end/"):
            self.status = "ended"
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def send(self, type_, **data):
        self.seq += 1
        self.commands.append({"seq": self.seq, "type": type_, "data": data})

    def types(self):
        return [e["type"] for e in self.events]


def make(relay, **kw):
    c = httpx.Client(transport=httpx.MockTransport(relay.handler))
    return rcmod.RemoteControl("sk-free-test", project="proj", model="m", client=c, host="box", **kw)


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_start_registers_and_saves_private_state(home):
    r = Relay()
    rc = make(r)
    rc.start()
    try:
        assert rc.session_id and rc.url
        assert oct(rcmod.STATE_FILE.stat().st_mode & 0o777) == "0o600"
        assert r.seen_headers[0]["authorization"] == "Bearer sk-free-test"
        assert wait_for(lambda: "status" in r.types())
    finally:
        rc.stop()
    assert r.status == "ended" and not rcmod.STATE_FILE.exists()


def test_events_flow_out_and_commands_flow_in(home):
    r = Relay()
    rc = make(r)
    rc.start()
    try:
        rc.emit("assistant", {"text": "hello"})
        assert wait_for(lambda: "assistant" in r.types())
        r.send("prompt", text="run the tests")
        assert wait_for(lambda: rc.next_remote_prompt() == "run the tests")
        r.send("interrupt")
        assert wait_for(rc.cancel.is_set)
    finally:
        rc.stop()


def test_end_from_the_app_stops_it(home):
    r = Relay()
    notes = []
    rc = make(r, notify=notes.append)
    rc.start()
    r.send("end")
    assert wait_for(lambda: rc.stopped.is_set())
    assert not rc.active and any("ended" in n for n in notes)


def test_relay_outage_keeps_the_session_local_and_recovers(home):
    r = Relay()
    notes = []
    rc = make(r, notify=notes.append)
    rc.start()
    try:
        assert wait_for(lambda: "status" in r.types())
        r.fail = True
        rc.emit("assistant", {"text": "while offline"})
        assert wait_for(lambda: not rc.connected, 5)
        assert rc.active and rc._out                        # still running, events buffered
        r.fail = False
        assert wait_for(lambda: any(e["data"].get("text") == "while offline" for e in r.events), 8)
        assert rc.connected and any("reconnected" in n for n in notes)
    finally:
        rc.stop()


def test_buffer_is_capped_while_offline(home):
    rc = rcmod.RemoteControl("k", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    rc.active = True
    for i in range(rcmod.BUFFER_MAX + 40):
        rc.emit("assistant", {"text": str(i)})
    assert len(rc._out) == rcmod.BUFFER_MAX and rc._out[0]["data"]["text"] == "40"


class FakeReader:
    def __init__(self, lines):
        self.lines = list(lines)

    def request(self, prompt=""):
        pass

    def get(self, timeout):
        return (True, self.lines.pop(0)) if self.lines else (False, None)


def test_approval_answered_from_the_app(home):
    r = Relay()
    rc = make(r)
    rc.active = True
    out = []

    def app():
        wait_for(lambda: rc._out)
        aid = rc._out[0]["data"]["id"]
        rc._handle("approve", {"approval_id": aid})

    threading.Thread(target=app, daemon=True).start()
    assert rc.confirm("Apply this change?", "file_write", "a.py", FakeReader([]), out.append) is True
    assert [e["type"] for e in rc._out] == ["approval_request", "status", "approval_resolved", "status"]
    assert rc._out[2]["data"]["by"] == "app"


def test_approval_answered_in_the_terminal_and_denial(home):
    rc = make(Relay())
    rc.active = True
    assert rc.confirm("m", "t", "p", FakeReader(["n"]), lambda m: None) is False
    assert rc.confirm("m", "t", "p", FakeReader([""]), lambda m: None) is True
    assert rc.confirm("m", "t", "p", FakeReader(["y"]), lambda m: None) is True


def test_app_deny_and_unknown_approval_ids_are_ignored(home):
    rc = make(Relay())
    rc.active = True
    rc._handle("approve", {"approval_id": "nope"})          # nothing pending: no effect, no crash
    threading.Thread(target=lambda: (wait_for(lambda: rc._out), rc._handle("deny", {"approval_id": rc._out[0]["data"]["id"]})), daemon=True).start()
    assert rc.confirm("m", "t", "p", FakeReader([]), lambda m: None) is False


def test_interrupt_denies_a_pending_approval(home):
    rc = make(Relay())
    rc.active = True
    threading.Thread(target=lambda: (wait_for(lambda: rc._out), rc.cancel.set()), daemon=True).start()
    assert rc.confirm("m", "t", "p", FakeReader([]), lambda m: None) is False


def test_redaction_and_previews():
    assert "sk-free-abcdefgh1234" not in rcmod.redact("key sk-free-abcdefgh1234 here")
    assert "hunter2hunter2" not in rcmod.redact("password=hunter2hunter2")
    assert rcmod.redact("x" * 5000).endswith("...") and len(rcmod.redact("x" * 5000)) < 900
    assert rcmod.args_preview("shell_command", {"command": "ls -la", "_project_root": "/x"}) == "ls -la"
    assert "file contents" not in rcmod.args_preview("file_write", {"path": "a.py", "content": "file contents"})


def test_stop_from_state_ends_the_recorded_session(home):
    r = Relay()
    rc = make(r)
    rc.start()
    rc.active = False                                           # simulate another terminal: state file remains
    rc.stopped.set()
    rcmod.STATE_FILE.write_text(json.dumps({"session_id": "11111111-1111-1111-1111-111111111111", "secret": "s3cret"}))
    msg = rcmod.stop_from_state("sk-free-test", client=httpx.Client(transport=httpx.MockTransport(r.handler)))
    assert "ended" in msg and r.status == "ended" and not rcmod.STATE_FILE.exists()
    assert "No Remote Control" in rcmod.stop_from_state("k")


class Scripted:
    model = "qwen7b"

    def __init__(self, replies):
        self.replies = list(replies)

    async def chat_stream(self, messages, system=None, tools=None):
        yield {"type": "text", "content": self.replies.pop(0)}
        yield {"type": "done"}


def agent_with_rc(tmp_path, replies, rc, **kw):
    a = Agent(tmp_path, {"safe_mode": True, "max_context_tokens": 2000}, output="print", **kw)
    a.client = Scripted(replies)
    a.rc = rc
    return a


def test_agent_emits_transcript_events(tmp_path, home):
    (tmp_path / "a.txt").write_text("hi")
    rc = make(Relay())
    rc.active = True
    a = agent_with_rc(tmp_path, [json.dumps({"tool": "file_read", "args": {"path": "a.txt"}}), "It says hi."], rc)
    asyncio.run(a.chat("read a.txt"))
    types = [e["type"] for e in rc._out]
    assert types == ["status", "tool_call", "tool_result", "assistant", "status"]
    assert rc._out[1]["data"]["tool"] == "file_read" and rc._out[2]["data"]["ok"] is True
    assert rc._out[0]["data"]["state"] == "working" and rc._out[-1]["data"]["state"] == "idle"


def test_remote_interrupt_stops_the_loop(tmp_path, home):
    rc = make(Relay())
    rc.active = True
    a = agent_with_rc(tmp_path, [json.dumps({"tool": "list_files", "args": {}})] * 5, rc)
    orig = a._execute_tool

    async def interrupting(*args, **kw):
        rc.cancel.set()                                         # the app taps Interrupt mid-task
        return await orig(*args, **kw)

    a._execute_tool = interrupting
    asyncio.run(a.chat("loop"))
    assert a.last_error == "interrupted" and len(a.tool_log) <= 1


def test_remote_approval_gates_a_file_write(tmp_path, home, monkeypatch):
    """The relay can approve, but the approval request is still raised and the write waits for it."""
    rc = make(Relay())
    rc.active = True
    a = Agent(tmp_path, {"safe_mode": True, "max_context_tokens": 2000}, output="rich")
    a.client = Scripted([json.dumps({"tool": "file_write", "args": {"path": "x.txt", "content": "1"}}), "done"])
    a.rc, a.reader = rc, FakeReader([])
    seen = {}

    def app():
        wait_for(lambda: any(e["type"] == "approval_request" for e in rc._out))
        seen["file_before_approval"] = (tmp_path / "x.txt").exists()
        aid = next(e["data"]["id"] for e in rc._out if e["type"] == "approval_request")
        rc._handle("approve", {"approval_id": aid})

    threading.Thread(target=app, daemon=True).start()
    asyncio.run(a.chat("write x"))
    assert seen["file_before_approval"] is False
    assert (tmp_path / "x.txt").read_text() == "1"


def test_remote_deny_blocks_the_write(tmp_path, home):
    rc = make(Relay())
    rc.active = True
    a = Agent(tmp_path, {"safe_mode": True, "max_context_tokens": 2000}, output="rich")
    a.client = Scripted([json.dumps({"tool": "shell_command", "args": {"command": "touch made.txt"}}), "ok"])
    a.rc, a.reader = rc, FakeReader([])
    threading.Thread(target=lambda: (wait_for(lambda: any(e["type"] == "approval_request" for e in rc._out)),
                                     rc._handle("deny", {"approval_id": next(e["data"]["id"] for e in rc._out if e["type"] == "approval_request")})),
                     daemon=True).start()
    asyncio.run(a.chat("make"))
    assert not (tmp_path / "made.txt").exists()


def test_next_input_prefers_a_remote_prompt(home):
    from free_code import cli
    rc = make(Relay())
    rc.active = True
    rc.commands.put("from the phone")

    class A:
        pass

    a = A()
    a.rc, a.reader = rc, rcmod.TerminalReader()
    assert cli._next_input(a, lambda p: "unused") == ("from the phone", True)


def test_next_input_without_rc_uses_the_normal_prompt():
    from free_code import cli

    class A:
        rc = None
        reader = None

    assert cli._next_input(A(), lambda p: "typed") == ("typed", False)


def test_rc_start_errors_are_clear(home):
    def deny(req):
        return httpx.Response(401, json={})
    rc = rcmod.RemoteControl("bad", client=httpx.Client(transport=httpx.MockTransport(deny)))
    with pytest.raises(rcmod.RCError, match="login"):
        rc.start()

    def full(req):
        return httpx.Response(409, json={"error": "Too many live remote sessions (5)."})
    rc = rcmod.RemoteControl("k", client=httpx.Client(transport=httpx.MockTransport(full)))
    with pytest.raises(rcmod.RCError, match="Too many"):
        rc.start()
