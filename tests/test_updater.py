import json
import time

import httpx

from free_code import updater as u
from free_code import __version__


def test_upgrade_command_per_install_method(monkeypatch):
    assert u.upgrade_command("uv") == ["uv", "tool", "upgrade", "freeai-code"]
    assert u.upgrade_command("uv", "0.3.0") == ["uv", "tool", "install", "--force", "freeai-code==0.3.0"]
    assert u.upgrade_command("pipx") == ["pipx", "upgrade", "freeai-code"]
    for m in ("pip", "venv", "npm"):
        cmd = u.upgrade_command(m)
        assert cmd[1:] == ["-m", "pip", "install", "--upgrade", "freeai-code"]


def test_run_update_dry_run_does_not_execute(capsys):
    assert u.run_update(dry_run=True, method="pip") == 0
    assert "pip install" in capsys.readouterr().out


def test_run_update_missing_tool(monkeypatch, capsys):
    monkeypatch.setattr(u.shutil, "which", lambda n: None)
    assert u.run_update(method="uv") == 1
    assert "install.sh" in capsys.readouterr().err


def pypi_client(version):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"info": {"version": version}})))


def test_notice_when_newer_and_checks_at_most_daily():
    n = u.daily_notice({}, now=2_000_000_000.0, client=pypi_client("99.0.0"), sync=True)
    assert "99.0.0" in n and "free-code update" in n
    # Second call within a day must not hit the network.
    boom = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("network"))))
    assert "99.0.0" in u.daily_notice({}, now=2_000_000_000.0 + 3600, client=boom, sync=True)


def test_no_notice_when_current():
    assert u.daily_notice({}, now=2_000_000_000.0, client=pypi_client(__version__), sync=True) is None


def test_opt_outs(monkeypatch):
    assert u.daily_notice({"update_check": False}, client=pypi_client("99.0.0"), sync=True) is None
    monkeypatch.setenv("FREE_CODE_NO_UPDATE_CHECK", "1")
    assert u.daily_notice({}, client=pypi_client("99.0.0"), sync=True) is None
    monkeypatch.delenv("FREE_CODE_NO_UPDATE_CHECK")
    monkeypatch.setenv("CI", "true")
    assert u.check_disabled({})


def test_notice_never_blocks_on_stale_state(monkeypatch):
    started = []
    monkeypatch.setattr(u.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: started.append(kw["name"])})())
    assert u.daily_notice({}, now=time.time()) is None   # stale/missing state: refresh happens in a thread
    assert started == ["free-code-update-check"]


def test_manifest_latest_version_is_used(home):
    from free_code import manifest as mf
    from tests_helpers import good_manifest
    mf.write_cache(mf.validate_manifest(good_manifest(latest_version="42.0.0"))[0], None)
    boom = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("pypi hit"))))
    assert "42.0.0" in u.daily_notice({}, now=2_000_000_000.0, client=boom, sync=True)


def test_min_version_notice(home):
    from free_code import manifest as mf
    from tests_helpers import good_manifest
    mf.write_cache(mf.validate_manifest(good_manifest(min_version="99.0.0", latest_version=None))[0], None)
    st = {"checked_at": time.time(), "latest": None}
    u.write_state(st)
    assert "minimum supported 99.0.0" in u.daily_notice({}, now=time.time(), sync=True)
