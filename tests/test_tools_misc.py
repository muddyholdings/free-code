import json

import httpx

from free_code import models, sessions
from free_code.tools import i18n, web


def mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_mask_unmask_roundtrip_and_slot_loss():
    masked, slots = i18n.mask("Hello {name}, you have %d items {0} <b>x</b>")
    assert "{name}" not in masked and len(slots) == 5
    assert i18n.unmask(masked, slots) == "Hello {name}, you have %d items {0} <b>x</b>"
    assert i18n.unmask("Hola {0}", slots) is None                  # dropped slots
    assert i18n.unmask("{0}{0}{1}{2}{3}{4}", slots) is None        # duplicated slot


def test_default_output_names():
    from pathlib import Path
    assert i18n.default_output(Path("l/en.json"), "en", "es") == Path("l/es.json")
    assert i18n.default_output(Path("l/app_en.json"), "en", "fr") == Path("l/app_fr.json")
    assert i18n.default_output(Path("l/messages.json"), "en", "de") == Path("l/messages.de.json")


def test_translate_file_keeps_english_when_slots_break(tmp_path):
    (tmp_path / "en.json").write_text(json.dumps({"a": "Save", "b": {"c": "Hello {name}"}, "d": "Total {0}"}))

    def handler(req):
        text = json.loads(req.content)["text"]
        out = {"Save": "Guardar", "Hello {0}": "Hola {0}", "Total {0}": "Total"}[text]   # last one loses its slot
        return httpx.Response(200, json={"translated_text": out})

    msg = i18n.translate_file("en.json", "es", _project_root=str(tmp_path), _client=mock(handler))
    out = json.loads((tmp_path / "es.json").read_text())
    assert out == {"a": "Guardar", "b": {"c": "Hola {name}"}, "d": "Total {0}"}
    assert "Translated 2 of 3" in msg and "d" in msg


def test_translate_file_limits_and_errors(tmp_path):
    assert i18n.translate_file("nope.json", "es", _project_root=str(tmp_path)).startswith("Error: file not found")
    (tmp_path / "a.json").write_text('{"a": "x"}')
    assert "outside the project" in i18n.translate_file("a.json", "es", output_path="../x.json", _project_root=str(tmp_path))
    out = i18n.translate_file("a.json", "es", _project_root=str(tmp_path), _client=mock(lambda r: httpx.Response(429, json={})))
    assert "429" in out and not (tmp_path / "a.es.json").exists() and not (tmp_path / "es.json").exists()


def test_web_fetch_strips_html_and_truncates():
    html = "<html><head><style>x{}</style><script>evil()</script></head><body><h1>Title</h1><p>Body &amp; more</p></body></html>"
    out = web.web_fetch("https://x.example/", _client=mock(lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"})))
    assert "Title" in out and "Body & more" in out and "evil" not in out and "x{}" not in out
    big = web.web_fetch("https://x.example/", max_chars=500, _client=mock(lambda r: httpx.Response(200, text="a" * 5000, headers={"content-type": "text/plain"})))
    assert "truncated" in big
    assert web.web_fetch("file:///etc/passwd").startswith("Error")
    assert web.web_fetch("https://x.example/", _client=mock(lambda r: httpx.Response(404))).startswith("Error: HTTP 404")


MODELS = [
    {"id": "qwen7b", "name": "Qwen", "type": "chat", "self_hosted": True, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-x", "name": "GPT X", "type": "chat", "self_hosted": False, "pricing": {"prompt": "0.000008", "completion": "0.00004"}},
    {"id": "sdxl", "name": "SDXL", "type": "image", "self_hosted": True},
]


def test_free_vs_paid_labels_are_honest():
    assert models.is_free(MODELS[0]) and not models.is_free(MODELS[1])
    assert models.tier(MODELS[2]) == "self-hosted" and "daily limits" in models.describe(MODELS[2])
    assert models.tier({"id": "x", "type": "chat"}) == "paid"      # unknown and not self-hosted: never claim free
    assert models.describe(MODELS[0]) == "free tier"
    assert "paid" in models.describe(MODELS[1]) and "$8 in" in models.describe(MODELS[1])


def test_filter_and_order_free_first():
    got = models.filter_models(MODELS, kind="chat")
    assert [m["id"] for m in got] == ["qwen7b", "openai/gpt-x"]
    assert [m["id"] for m in models.filter_models(MODELS, free_only=True)] == ["qwen7b", "sdxl"]
    assert [m["id"] for m in models.filter_models(MODELS, query="gpt")] == ["openai/gpt-x"]


def test_fetch_models_caches_and_falls_back(home):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"models": MODELS})

    assert len(models.fetch_models(client=mock(handler))) == 3
    assert len(models.fetch_models(client=mock(handler))) == 3 and calls["n"] == 1   # cached
    def down(req):
        raise httpx.ConnectError("x")
    assert len(models.fetch_models(force=True, client=mock(down))) == 3              # stale cache beats nothing
    models.MODELS_CACHE.unlink()
    assert models.fetch_models(client=mock(down)) == models.FREE_AI_MODELS             # built-in list last


def test_sessions_prune_and_safe_ids(tmp_path):
    d = tmp_path / "s"
    for i in range(sessions.KEEP + 3):
        sessions.save(f"id{i:03d}", tmp_path, "m", [{"role": "user", "content": "hi"}], directory=d)
    assert len(list(d.glob("*.json"))) == sessions.KEEP
    assert sessions.load("../etc/passwd", directory=d) is None
    p = sessions.save("zz", tmp_path, "m", [{"role": "user", "content": "x"}], directory=d)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert sessions.load("z", directory=d)["id"] == "zz"          # unique prefix
