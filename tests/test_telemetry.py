import json
import sys

import httpx
import pytest

from free_code import telemetry as t


def transport_client(status=201, sink=None):
    def handler(req):
        if sink is not None:
            sink.append(json.loads(req.content))
        return httpx.Response(status, json={"ok": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_default_is_unset_and_sends_nothing():
    assert t.status() == "unset" and not t.enabled()
    sink = []
    assert t.submit(t.build_rating(True), client=transport_client(sink=sink)) is False
    assert sink == []


def test_opt_in_then_out():
    t.set_enabled(True)
    assert t.enabled()
    sink = []
    assert t.submit(t.build_rating(False, "too slow"), client=transport_client(sink=sink))
    assert sink[0]["kind"] == "rating" and sink[0]["rating"] == -1 and sink[0]["origin"] == "cli"
    t.set_enabled(False)
    assert t.status() == "off"
    assert not t.submit(t.build_rating(True), client=transport_client(sink=sink)) and len(sink) == 1


@pytest.mark.parametrize("var", ["FREE_CODE_NO_TELEMETRY", "DO_NOT_TRACK"])
def test_env_forces_off_even_when_on(monkeypatch, var):
    t.set_enabled(True)
    monkeypatch.setenv(var, "1")
    assert t.status() == "off"
    sink = []
    assert not t.submit(t.build_rating(True), force=True, client=transport_client(sink=sink))
    assert sink == []


def test_args_shape_never_contains_values():
    secret = "sk-free-SUPERSECRETVALUE and /home/me/code.py"
    shape = t.args_shape({"command": secret, "path": "/home/me/x", "n": 5, "flag": True, "items": [1, 2], "_project_root": "/x"})
    blob = json.dumps(shape)
    assert "SECRET" not in blob and "/home" not in blob and "_project_root" not in shape
    assert shape["n"] == "int" and shape["flag"] == "bool" and shape["items"] == "list:2" and shape["command"].startswith("str:")


def test_scrub_message_removes_quotes_paths_numbers_tokens():
    msg = "No such file: '/home/alice/secret_plan.txt' at line 42 key sk-free-abcdefghijklmnopqrstuvwxyz0123"
    out = t.scrub_message(msg)
    assert "alice" not in out and "secret_plan" not in out and "42" not in out and "abcdefgh" not in out
    assert t.scrub_message("line one\nsecond line with 'data'") == "line one"


def test_crash_report_has_only_own_frames_and_scrubbed_message():
    try:
        from free_code import sessions
        sessions.load.__wrapped__  # AttributeError raised inside our own package call chain below
    except AttributeError as e:
        ev = t.build_crash(e, "qwen7b")
    assert ev["kind"] == "crash" and ev["error_class"] == "AttributeError"
    assert all(f.count(":") == 2 for f in ev["frames"])
    assert "version" in ev and "os" in ev and "python" in ev
    assert set(ev) <= {"origin", "version", "os", "python", "install", "kind", "error_class", "message", "frames", "model"}


def test_tool_failure_event_shape_and_mcp_name_hidden():
    ev = t.build_tool_failure("file_read", "Error: File not found: /a/b", {"path": "/a/b"}, "m")
    assert ev["error_class"] == "not_found" and ev["args_shape"] == {"path": "str:1-80"} and "/a/b" not in json.dumps(ev)
    ev2 = t.build_tool_failure("mcp__secret_server__tool", "Error: x", {}, "m", source="mcp")
    assert ev2["tool"] == "mcp_tool" and "secret_server" not in json.dumps(ev2)


def test_classify_error():
    assert t.classify_error("Error: API 429: limit") == "rate_limited"
    assert t.classify_error("Error: Command timed out after 60s") == "timeout"
    assert t.classify_error("Error: something odd") == "other"


def test_failed_send_is_queued_and_flushed():
    t.set_enabled(True)
    assert t.submit(t.build_rating(True), client=transport_client(status=500))
    assert t.QUEUE_FILE.exists()
    sink = []
    assert t.flush_queue(transport_client(sink=sink)) == 1 and not t.QUEUE_FILE.exists() and sink[0]["kind"] == "rating"


def test_dry_run_prints_and_sends_nothing(capsys):
    t.set_enabled(True)
    import os
    os.environ["FREE_CODE_TELEMETRY_DRYRUN"] = "1"
    try:
        sink = []
        assert t.submit(t.build_rating(True), client=transport_client(sink=sink))
    finally:
        del os.environ["FREE_CODE_TELEMETRY_DRYRUN"]
    assert sink == [] and "dry run" in capsys.readouterr().err


def test_feedback_attachment_only_when_given():
    assert t.build_feedback("hi")["attached"] is False and t.build_feedback("hi")["attachment"] == ""
    ev = t.build_feedback("hi", attachment="USER: x", email="a@b.co")
    assert ev["attached"] is True and ev["email"] == "a@b.co"


def test_explicit_feedback_works_when_automatic_is_off():
    t.set_enabled(False)
    sink = []
    assert t.submit(t.build_feedback("it broke"), force=True, client=transport_client(sink=sink))
    assert sink[0]["kind"] == "feedback"


def test_consent_default_is_no():
    assert t.ask_consent(lambda p: "") is False and t.status() == "off"
    assert t.ask_consent(lambda p: "y") is True and t.status() == "on"


def test_unknown_kind_dropped():
    t.set_enabled(True)
    assert not t.submit({"kind": "keylog"}, client=transport_client())


def test_report_exception_never_raises():
    t.set_enabled(True)
    t.report_exception(ValueError("x"), wait=False)
