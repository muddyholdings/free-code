import httpx
import pytest

from free_code import auth, config


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_full_flow_stores_key_with_mode_600(home, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    monkeypatch.setattr(auth.webbrowser, "open", lambda u: True)
    polls = {"n": 0}

    def handler(req):
        if req.url.path.endswith("/login/start/"):
            return httpx.Response(201, json={"code": "ABCD-2345", "poll_token": "p" * 30,
                                             "verification_url": "https://free.ai/code/login/?code=ABCD-2345",
                                             "expires_in": 600, "interval": 2})
        polls["n"] += 1
        if polls["n"] < 3:
            return httpx.Response(202, json={"status": "pending"})
        return httpx.Response(200, json={"status": "approved", "api_key": "sk-free-xyz", "name": "free-code on box", "email": "a@b.co"})

    assert auth.browser_login(client=client(handler)) is True
    assert config.load_config()["token"] == "sk-free-xyz"
    assert oct(config.CONFIG_FILE.stat().st_mode & 0o777) == "0o600"
    assert oct(config.CONFIG_DIR.stat().st_mode & 0o777) == "0o700"


def test_denied_or_expired(home, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    monkeypatch.setattr(auth.webbrowser, "open", lambda u: True)

    def handler(req):
        if req.url.path.endswith("/login/start/"):
            return httpx.Response(201, json={"code": "ABCD-2345", "poll_token": "p" * 30, "verification_url": "u", "expires_in": 600})
        return httpx.Response(410, json={"status": "expired"})

    assert auth.browser_login(client=client(handler)) is False
    assert config.load_config()["token"] is None


def test_poll_slow_down_then_key_limit(monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    seq = iter([httpx.Response(429, json={}), httpx.Response(409, json={"error": "5 keys"})])
    with pytest.raises(auth.LoginFailed, match="5 keys"):
        auth.poll_login(client(lambda r: next(seq)), "t" * 30, 600)


def test_poll_times_out(monkeypatch):
    ticks = iter(range(0, 10000, 400))
    with pytest.raises(auth.LoginFailed, match="Timed out"):
        auth.poll_login(client(lambda r: httpx.Response(202, json={})), "t" * 30, 600,
                        sleep=lambda s: None, clock=lambda: next(ticks))


def test_start_rate_limited():
    with pytest.raises(auth.LoginFailed, match="Too many"):
        auth.start_login(client(lambda r: httpx.Response(429, json={})))


def test_network_down_returns_false(home):
    def boom(req):
        raise httpx.ConnectError("down")
    assert auth.browser_login(client=client(boom)) is False
