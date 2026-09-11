"""OpenRouter allowlist validation (one boundary) and plumbing into the backend."""
import pytest

from podracer import config as config_mod
from podracer.config import load_config
from podracer.process import _build_summarize_backend
from podracer.providers import validate_allowlist
from podracer.summarize import Backend


@pytest.mark.parametrize("value,expected", [
    (None, None),
    (["deepinfra", " digitalocean "], ["deepinfra", "digitalocean"]),
])
def testvalidate_allowlist(value, expected):
    assert validate_allowlist(value) == expected


def test_empty_allowlist_is_an_error_not_unrestricted():
    # Fail closed: `[]` must not silently turn the allowlist off — at the
    # config boundary and at the Backend factory alike.
    with pytest.raises(ValueError, match="empty"):
        validate_allowlist([])
    with pytest.raises(ValueError, match="empty"):
        Backend.openrouter("m", api_key="x", providers=[])


def test_all_denylisted_allowlist_is_an_error():
    # only=["baidu"] + ignore=["Baidu"] would 404 on every call.
    with pytest.raises(ValueError, match="denylisted"):
        validate_allowlist(["baidu"])
    assert validate_allowlist(["baidu", "deepinfra"]) == ["baidu", "deepinfra"]


@pytest.mark.parametrize("value", ["deepinfra", [1], [""], ["ok", None]])
def testvalidate_allowlist_rejects_non_slug_lists(value):
    with pytest.raises(ValueError):
        validate_allowlist(value)


def test_allowlist_flows_from_toml_to_backend(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[summarize]\nbackend = "openrouter"\nmodel = "m"\n'
        'openrouter_providers = ["deepinfra", "digitalocean"]\n'
        '[keys]\nopenrouter_api_key = "x"\n',
    )
    monkeypatch.setattr(config_mod, "_find_config_file", lambda: tmp_path / "config.toml")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = load_config()
    assert cfg.summarize_openrouter_providers == ["deepinfra", "digitalocean"]
    backend = _build_summarize_backend(cfg, None, None)
    assert backend.providers == ["deepinfra", "digitalocean"]
