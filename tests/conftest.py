"""Shared fixtures: every test runs against a throwaway HOME, never ~/.free-code."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".free-code"
    from free_code import auth, config, manifest, mcp, models, rc, sessions, telemetry, updater

    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_dir / "config.yaml")
    monkeypatch.setattr(manifest, "CACHE_FILE", cfg_dir / "manifest.json")
    monkeypatch.setattr(mcp, "USER_MCP_FILE", cfg_dir / "mcp.json")
    monkeypatch.setattr(mcp, "TRUST_FILE", cfg_dir / "mcp-trusted.json")
    monkeypatch.setattr(models, "MODELS_CACHE", cfg_dir / "models.json")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", cfg_dir / "sessions")
    monkeypatch.setattr(telemetry, "QUEUE_FILE", cfg_dir / "telemetry-queue.jsonl")
    monkeypatch.setattr(updater, "STATE_FILE", cfg_dir / "state.json")
    monkeypatch.setattr(rc, "STATE_FILE", cfg_dir / "rc.json")
    for var in ("FREE_CODE_NO_TELEMETRY", "DO_NOT_TRACK", "FREE_CODE_NO_MANIFEST", "FREE_CODE_NO_UPDATE_CHECK",
                "CI", "FREE_CODE_TELEMETRY_DRYRUN", "FREE_CODE_MANIFEST_URL", "FREE_CODE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return cfg_dir
