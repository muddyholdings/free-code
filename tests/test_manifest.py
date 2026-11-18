import json
import time

import httpx
import pytest

from free_code import manifest as mf
from free_code.builtin_manifest import BUILTIN_MANIFEST


def good(**over):
    m = {"schema": 1, "manifest_version": 5, "latest_version": "9.9.9", "min_version": None,
         "disabled_tools": ["generate_video"], "tool_notes": {"web_search": "Keep queries short."},
         "tools": [{
             "name": "make_qr", "description": "Make a QR code image",
             "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "out": {"type": "string"}}, "required": ["text", "out"]},
             "http": {"method": "POST", "path": "/v1/qr/", "body": {"text": "{text}"}},
             "result": {"file_url": ["url"], "save_arg": "out"}, "confirm": "write"}]}
    m.update(over)
    return m


def test_validate_ok_and_per_tool_rejection():
    bad_tool = {"name": "shell_command", "description": "nope nope", "parameters": {"type": "object", "properties": {}},
                "http": {"method": "POST", "path": "/v1/x/"}}
    clean, rejected = mf.validate_manifest(good(tools=good()["tools"] + [bad_tool]))
    assert [t["name"] for t in clean["tools"]] == ["make_qr"]
    assert rejected and "local tool" in rejected[0]


@pytest.mark.parametrize("data", [
    [], {"schema": 2, "manifest_version": 1}, {"schema": 1, "manifest_version": 0},
    {"schema": 1, "manifest_version": 1, "latest_version": "abc"},
    {"schema": 1, "manifest_version": 1, "disabled_tools": ["Bad Name"]},
    {"schema": 1, "manifest_version": True},
])
def test_validate_rejects_whole_manifest(data):
    with pytest.raises(mf.ManifestError):
        mf.validate_manifest(data)


def test_builtin_is_a_valid_manifest():
    clean, rejected = mf.validate_manifest(BUILTIN_MANIFEST)
    assert not rejected and len(clean["tools"]) == len(BUILTIN_MANIFEST["tools"])


def test_merge_adds_overrides_and_disables():
    clean, _ = mf.validate_manifest(good())
    eff = mf.merge(BUILTIN_MANIFEST, clean)
    names = {t["name"] for t in eff["tools"]}
    assert "make_qr" in names and "generate_image" in names
    assert "generate_video" in eff["disabled_tools"]
    assert eff["tool_notes"]["web_search"] == "Keep queries short."
    assert eff["latest_version"] == "9.9.9"


def test_refresh_writes_cache_and_load_uses_it():
    def handler(req):
        assert req.url.scheme == "https"
        return httpx.Response(200, json=good(), headers={"etag": '"abc"'})

    status, _ = mf.refresh({}, force=True, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert status.startswith("updated")
    assert "make_qr" in {t["name"] for t in mf.load({})["tools"]}


def test_older_manifest_is_ignored():
    c = lambda body: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    mf.refresh({}, force=True, client=c(good(manifest_version=7)))
    status, _ = mf.refresh({}, force=True, client=c(good(manifest_version=3, tools=[])))
    assert "rollback" in status
    assert mf.read_cache()["manifest"]["manifest_version"] == 7


def test_offline_falls_back_to_builtin():
    def boom(req):
        raise httpx.ConnectError("down")

    status, _ = mf.refresh({}, force=True, client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert "could not refresh" in status
    assert {t["name"] for t in mf.load({})["tools"]} == {t["name"] for t in BUILTIN_MANIFEST["tools"]}


def test_garbage_response_does_not_replace_cache():
    mf.refresh({}, force=True, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=good()))))
    status, _ = mf.refresh({}, force=True, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))))
    assert "could not refresh" in status
    assert mf.read_cache()["manifest"]["manifest_version"] == 5


def test_opt_out_env_and_config(monkeypatch):
    assert mf.refresh({"manifest": False}, force=True)[0] == "manifest fetch is off"
    monkeypatch.setenv("FREE_CODE_NO_MANIFEST", "1")
    assert mf.manifest_disabled({})
    assert mf.load({}) == BUILTIN_MANIFEST


def test_non_https_override_ignored(monkeypatch):
    monkeypatch.setenv("FREE_CODE_MANIFEST_URL", "http://evil.example/m.json")
    assert mf.manifest_url() == mf.MANIFEST_URL


def test_stale_cache_refetched_with_etag(home):
    mf.write_cache(mf.validate_manifest(good())[0], '"abc"')
    rec = json.loads(mf.CACHE_FILE.read_text())
    rec["fetched_at"] = time.time() - 7 * 3600
    mf.CACHE_FILE.write_text(json.dumps(rec))
    seen = {}

    def handler(req):
        seen["inm"] = req.headers.get("if-none-match")
        return httpx.Response(304)

    status, _ = mf.refresh({}, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert status == "not modified" and seen["inm"] == '"abc"'


def test_is_newer():
    assert mf.is_newer("0.2.0", "0.1.9") and not mf.is_newer("0.1.9", "0.1.9") and not mf.is_newer(None) and not mf.is_newer("junk")
