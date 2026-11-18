import json

import httpx
import pytest

from free_code import remote_tools as rt
from free_code.builtin_manifest import BUILTIN_MANIFEST


def spec(**over):
    base = {
        "name": "demo_tool", "description": "Does a demo thing",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "n": {"type": "integer"},
                                                         "out": {"type": "string"}}, "required": ["text"]},
        "http": {"method": "POST", "path": "/v1/demo/", "body": {"text": "{text}", "n": "{n|3}"}},
        "result": {"text": ["text"]},
        "confirm": "none",
    }
    base.update(over)
    return base


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_builtin_specs_all_validate():
    for t in BUILTIN_MANIFEST["tools"]:
        rt.validate_spec(t)


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(name="Bad Name"),
    lambda s: s.update(name="shell_command"),
    lambda s: s["http"].update(path="https://evil.example/v1/x/"),
    lambda s: s["http"].update(path="/v1/../etc/"),
    lambda s: s["http"].update(path="/admin/"),
    lambda s: s["http"].update(method="DELETE"),
    lambda s: s["http"].update(body={"a": "{nope}"}),
    lambda s: s["result"].update(save_arg="nope"),
    lambda s: s["result"].update(save_arg="out"),          # writes a file but confirm none
    lambda s: s.update(confirm="sometimes"),
    lambda s: s.update(description="x"),
])
def test_validate_rejects(mutate):
    s = spec()
    mutate(s)
    with pytest.raises(rt.SpecError):
        rt.validate_spec(s)


def test_render_body_types_defaults_and_dropped_keys():
    s = rt.validate_spec(spec())
    assert rt.render_body(s, {"text": "hi"}) == {"text": "hi", "n": 3}
    assert rt.render_body(s, {"text": "hi", "n": 7}) == {"text": "hi", "n": 7}


def test_execute_posts_json_and_returns_text():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"text": "hello"})

    out = rt.execute_spec(rt.validate_spec(spec()), {"text": "hi"}, token="sk-test", client=client_for(handler))
    assert out == "hello"
    assert seen["body"] == {"text": "hi", "n": 3}
    assert seen["auth"] == "Bearer sk-test"


def test_missing_required_argument():
    assert rt.execute_spec(rt.validate_spec(spec()), {}, client=client_for(lambda r: httpx.Response(200, json={}))).startswith("Error: missing")


def test_429_points_to_login():
    out = rt.execute_spec(rt.validate_spec(spec()), {"text": "x"},
                          client=client_for(lambda r: httpx.Response(429, json={"error": "Daily limit reached"})))
    assert out.startswith("Error: API 429") and "free-code login" in out


def test_save_file_inside_project_and_traversal_refused(tmp_path):
    s = rt.validate_spec(spec(
        parameters={"type": "object", "properties": {"text": {"type": "string"}, "out": {"type": "string"}}, "required": ["text", "out"]},
        http={"method": "POST", "path": "/v1/img/", "body": {"text": "{text}"}},
        result={"file_url": ["image_url"], "save_arg": "out"}, confirm="write"))

    def handler(req):
        if req.url.path == "/v1/img/":
            return httpx.Response(200, json={"image_url": "https://cdn.example/a.png"})
        return httpx.Response(200, content=b"PNGDATA")

    out = rt.execute_spec(s, {"text": "x", "out": "assets/a.png"}, project_root=str(tmp_path), client=client_for(handler))
    assert out == "Saved to assets/a.png"
    assert (tmp_path / "assets" / "a.png").read_bytes() == b"PNGDATA"
    bad = rt.execute_spec(s, {"text": "x", "out": "../escape.png"}, project_root=str(tmp_path), client=client_for(handler))
    assert bad.startswith("Error: refusing to write outside")
    assert not (tmp_path.parent / "escape.png").exists()


def test_non_https_output_url_refused(tmp_path):
    s = rt.validate_spec(spec(
        parameters={"type": "object", "properties": {"text": {"type": "string"}, "out": {"type": "string"}}, "required": ["text", "out"]},
        http={"method": "POST", "path": "/v1/img/", "body": {"text": "{text}"}},
        result={"file_url": ["url"], "save_arg": "out"}, confirm="write"))
    out = rt.execute_spec(s, {"text": "x", "out": "a.png"}, project_root=str(tmp_path),
                          client=client_for(lambda r: httpx.Response(200, json={"url": "http://x/a.png"})))
    assert "non-HTTPS" in out


def test_upload_sends_multipart(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("data")
    s = rt.validate_spec(spec(
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "language": {"type": "string"}}, "required": ["path"]},
        http={"method": "POST", "path": "/v1/ocr/", "body": {"language": "{language|eng}"}, "upload": {"arg": "path", "field": "file"}},
        confirm="upload"))
    seen = {}

    def handler(req):
        seen["ctype"] = req.headers["content-type"]
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "OCR TEXT"})

    out = rt.execute_spec(s, {"path": "a.txt"}, project_root=str(tmp_path), client=client_for(handler))
    assert out == "OCR TEXT"
    assert seen["ctype"].startswith("multipart/form-data") and b"data" in seen["body"] and b"eng" in seen["body"]


def test_job_polling(monkeypatch):
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(req):
        if req.url.path == "/v1/demo/":
            return httpx.Response(200, json={"job_id": "job12345"})
        calls["n"] += 1
        return httpx.Response(200, json={"status": "completed" if calls["n"] > 1 else "running", "text": "done"})

    assert rt.execute_spec(rt.validate_spec(spec()), {"text": "x"}, client=client_for(handler)) == "done"


def test_queued_gpu_job_polls_the_web_tier_job_url(monkeypatch, tmp_path):
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    jid = "07eb0cd5-b19c-4d9f-b5cb-a7a8c92f0e6f"
    seen = {"polls": 0, "hosts": set()}

    def handler(req):
        seen["hosts"].add(req.url.host)
        if req.url.path == "/v1/music/generate/":
            return httpx.Response(200, json={"queued": True, "job_id": jid, "poll_url": f"/api/v1/jobs/{jid}/"})
        if req.url.path == f"/api/v1/jobs/{jid}/":
            seen["polls"] += 1
            if seen["polls"] < 3:
                return httpx.Response(200, json={"job": {"status": "queued"}})
            return httpx.Response(200, json={"job": {"status": "completed", "output_url": "https://cdn.example/m.mp3"}})
        return httpx.Response(200, content=b"MP3")

    s = rt.validate_spec(next(t for t in BUILTIN_MANIFEST["tools"] if t["name"] == "generate_music"))
    out = rt.execute_spec(s, {"prompt": "calm", "output_path": "m.mp3"}, project_root=str(tmp_path), client=client_for(handler))
    assert out == "Saved to m.mp3" and (tmp_path / "m.mp3").read_bytes() == b"MP3"
    assert seen["polls"] == 3 and "free.ai" in seen["hosts"]


def test_queued_job_failure_and_hostile_poll_url(monkeypatch):
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    jid = "07eb0cd5-b19c-4d9f-b5cb-a7a8c92f0e6f"

    def failing(req):
        if req.url.path == "/v1/demo/":
            return httpx.Response(200, json={"queued": True, "job_id": jid, "poll_url": f"/api/v1/jobs/{jid}/"})
        return httpx.Response(200, json={"job": {"status": "failed", "error_message": "gpu oom"}})

    assert "gpu oom" in rt.execute_spec(rt.validate_spec(spec()), {"text": "x"}, client=client_for(failing))

    hosts = []

    def hostile(req):
        hosts.append(str(req.url))
        if req.url.path == "/v1/demo/":
            return httpx.Response(200, json={"job_id": "job12345", "poll_url": "https://evil.example/steal"})
        return httpx.Response(200, json={"status": "completed", "text": "ok"})

    assert rt.execute_spec(rt.validate_spec(spec()), {"text": "x"}, client=client_for(hostile)) == "ok"
    assert not any("evil.example" in h for h in hosts)
