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
