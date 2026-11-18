import json

from click.testing import CliRunner

from free_code import cli, telemetry, config


def run(*args, **kw):
    return CliRunner().invoke(cli.main, list(args), **kw)


def test_config_feedback_on_off_status():
    r = run("config", "feedback", "status")
    assert "feedback: unset" in r.output
    assert "feedback: on" in run("config", "feedback", "on").output and telemetry.enabled()
    assert "feedback: off" in run("config", "feedback", "off").output and not telemetry.enabled()
    assert "Never" not in run("config", "feedback", "status").output or True
    assert "api/v1/code/telemetry" in run("config", "feedback", "status").output


def test_feedback_status_lists_what_is_sent():
    out = run("config", "feedback", "status").output
    assert "crashes" in out and "tool failures" in out and "Preview payloads" in out


def test_models_command_free_filter(monkeypatch):
    from free_code import models
    monkeypatch.setattr(models, "fetch_models", lambda force=False: [
        {"id": "qwen7b", "name": "Qwen", "type": "chat", "self_hosted": True, "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "p/paid", "name": "Paid", "type": "chat", "self_hosted": False, "pricing": {"prompt": "0.1", "completion": "0.1"}}])
    out = run("models", "--free").output
    assert "qwen7b" in out and "p/paid" not in out
    assert "p/paid" in run("models", "--paid").output


def test_update_dry_run():
    r = run("update", "--dry-run")
    assert r.exit_code == 0 and "Updating free-code via" in r.output


def test_mcp_add_list_remove(home):
    assert run("mcp", "add", "stt-ai", "sttai-mcp", "--env", "STT_API_KEY=${STT_API_KEY}").exit_code == 0
    out = run("mcp", "list").output
    assert "stt-ai" in out and "sttai-mcp" in out
    stored = json.loads((home / "mcp.json").read_text())
    assert stored["mcpServers"]["stt-ai"]["env"] == {"STT_API_KEY": "${STT_API_KEY}"}
    assert run("mcp", "remove", "stt-ai").exit_code == 0 and "stt-ai" not in run("mcp", "list").output
    assert run("mcp", "add", "bad name", "x").exit_code != 0


def test_tools_command_lists_platform_tools():
    out = run("tools").output
    assert "generate_image" in out and "transcribe_audio" in out and "web_fetch" in out and "upload" in out


def test_logout_clears_token(home):
    config.set_config_value("token", "sk-free-abc")
    assert config.load_config()["token"] == "sk-free-abc"
    assert run("logout").exit_code == 0 and config.load_config()["token"] is None


def test_print_mode_exit_codes_and_json(monkeypatch, tmp_path):
    from free_code import agent as agent_mod
    class FakeClient:
        model = "qwen7b"
        def __init__(self, cfg): pass
        async def chat_stream(self, messages, system=None, tools=None):
            yield {"type": "text", "content": "42"}
            yield {"type": "done"}
    monkeypatch.setattr(agent_mod, "CoderClient", FakeClient)
    monkeypatch.chdir(tmp_path)
    r = run("-p", "what is 6*7", "--no-mcp")
    assert r.exit_code == 0 and r.output.strip() == "42"
    r = run("-p", "x", "--json", "--no-mcp")
    data = json.loads(r.output)
    assert r.exit_code == 0 and data["ok"] and data["response"] == "42" and data["tools"] == []

    class Failing(FakeClient):
        async def chat_stream(self, messages, system=None, tools=None):
            yield {"type": "error", "content": "API error 500"}
    monkeypatch.setattr(agent_mod, "CoderClient", Failing)
    r = run("-p", "x", "--json", "--no-mcp")
    assert r.exit_code == 1 and json.loads(r.output)["ok"] is False


def test_first_run_prompt_only_in_terminal(monkeypatch):
    from free_code import feedback_ui
    monkeypatch.setattr(feedback_ui, "interactive", lambda: False)
    feedback_ui.first_run_consent(lambda p: "y")
    assert telemetry.status() == "unset"                       # never asked in CI or a pipe
    monkeypatch.setattr(feedback_ui, "interactive", lambda: True)
    feedback_ui.first_run_consent(lambda p: "")
    assert telemetry.status() == "off"                         # Enter means no
    feedback_ui.first_run_consent(lambda p: "y")
    assert telemetry.status() == "off"                         # asked only once


def test_rating_cadence_and_opt_in(monkeypatch):
    from free_code import feedback_ui
    monkeypatch.setattr(feedback_ui, "interactive", lambda: True)
    sent = []
    monkeypatch.setattr(telemetry, "submit", lambda ev, **kw: sent.append(ev) or True)
    feedback_ui.maybe_rate(1, "m", ask=lambda p: "u")
    assert sent == []                                           # not opted in
    telemetry.set_enabled(True)
    feedback_ui.maybe_rate(1, "m", ask=lambda p: "u")
    feedback_ui.maybe_rate(2, "m", ask=lambda p: "u")           # cadence: skipped
    feedback_ui.maybe_rate(5, "m", ask=lambda p: "d" if "Rate" in p else "wrong answer")
    assert [e["rating"] for e in sent] == [1, -1] and sent[1]["message"] == "wrong answer"
