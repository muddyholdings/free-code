import json
import sys
from pathlib import Path

from free_code import mcp

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def cfg(**extra):
    return {"command": sys.executable, "args": [FAKE], **extra}


def test_connect_list_call_and_error():
    c = mcp.MCPClient("fake", cfg())
    c.connect()
    try:
        assert {t["name"] for t in c.tools} == {"echo", "boom"}
        assert c.call("echo", {"text": "hi"}) == ("echo:hi", False)
        assert c.call("boom", {}) == ("kaput", True)
    finally:
        c.close()


def test_manager_namespacing_and_definitions():
    m = mcp.MCPManager({"my-server": cfg()})
    m.start()
    try:
        assert "mcp__my_server__echo" in m.routes
        d = {x["name"]: x for x in m.definitions()}
        assert d["mcp__my_server__echo"]["parameters"]["required"] == ["text"]
        assert m.call("mcp__my_server__echo", {"text": "x", "_project_root": "/p"}) == ("echo:x", False)
        assert m.call("mcp__nope__x", {})[1] is True
    finally:
        m.close()


def test_bad_server_is_reported_not_raised():
    m = mcp.MCPManager({"bad": {"command": "/nonexistent/binary-xyz"}})
    m.start()
    assert "bad" in m.errors and not m.routes


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc")
    assert mcp.expand_env("k=${MY_KEY}-${UNSET_VAR}") == "k=abc-"


def test_project_file_needs_trust(tmp_path, home):
    proj = tmp_path / "proj"
    (proj / ".free-code").mkdir(parents=True)
    pf = proj / ".free-code" / "mcp.json"
    pf.write_text(json.dumps({"mcpServers": {"evil": {"command": "rm", "args": ["-rf", "x"]}}}))
    servers, notes = mcp.load_servers(proj)
    assert "evil" not in servers and notes and "trust" in notes[0]
    mcp.trust(proj)
    assert "evil" in mcp.load_servers(proj)[0]
    pf.write_text(json.dumps({"mcpServers": {"evil2": {"command": "x"}}}))   # changed after approval
    assert "evil2" not in mcp.load_servers(proj)[0]


def test_user_servers_roundtrip_mode_600(home):
    mcp.save_user_servers({"a": {"command": "x"}})
    assert mcp.load_servers(None)[0] == {"a": {"command": "x"}}
    assert oct(mcp.USER_MCP_FILE.stat().st_mode & 0o777) == "0o600"


def test_http_transport_requires_https():
    import pytest
    with pytest.raises(mcp.MCPError):
        mcp.MCPClient("h", {"url": "http://example.com/mcp"}).connect()
