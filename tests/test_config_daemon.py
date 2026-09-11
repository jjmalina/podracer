"""[daemon] auto-retry keys: loaded from config.toml, defaults when absent."""
import pytest

from podracer import config as config_mod
from podracer.config import load_config
from podracer.db.jobs import DEFAULT_AUTO_RETRY_COOLDOWN_HOURS, DEFAULT_AUTO_RETRY_PIPELINES


def _load_with(tmp_path, monkeypatch, toml: str):
    (tmp_path / "config.toml").write_text(toml)
    monkeypatch.setattr(config_mod, "_find_config_file", lambda: tmp_path / "config.toml")
    return load_config()


def test_auto_retry_keys_load_from_daemon_section(tmp_path, monkeypatch):
    cfg = _load_with(
        tmp_path, monkeypatch,
        "[daemon]\nauto_retry_pipelines = 5\nauto_retry_cooldown_hours = 12\n",
    )
    assert cfg.auto_retry_pipelines == 5
    assert cfg.auto_retry_cooldown_hours == 12


def test_auto_retry_keys_default_when_absent(tmp_path, monkeypatch):
    cfg = _load_with(tmp_path, monkeypatch, "[daemon]\nmax_attempts = 3\n")
    assert cfg.auto_retry_pipelines == 3
    assert cfg.auto_retry_cooldown_hours == 6
    # The DB-layer defaults (used by direct callers of find_new_episodes)
    # must agree with the config defaults, or "no config" and "default config"
    # would mean different policies.
    assert DEFAULT_AUTO_RETRY_PIPELINES == cfg.auto_retry_pipelines
    assert DEFAULT_AUTO_RETRY_COOLDOWN_HOURS == cfg.auto_retry_cooldown_hours


@pytest.mark.parametrize("toml,match", [
    ("[daemon]\nauto_retry_pipelines = 0\n", "auto_retry_pipelines"),
    ("[daemon]\nauto_retry_pipelines = -1\n", "auto_retry_pipelines"),
    ("[daemon]\nauto_retry_cooldown_hours = -6\n", "auto_retry_cooldown_hours"),
])
def test_auto_retry_keys_reject_values_that_disable_discovery(tmp_path, monkeypatch, toml, match):
    # budget 0 would make count(failed)=0 < 0 false for brand-new episodes and
    # stop the worker enqueueing anything; a negative cooldown builds a
    # '--N hours' modifier SQLite evaluates to NULL, silently disabling it.
    with pytest.raises(ValueError, match=match):
        _load_with(tmp_path, monkeypatch, toml)
