import podracer.sentry_config as sc


def _spy_init(monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "_configured_dsn", None)
    monkeypatch.setattr(sc.sentry_sdk, "init", lambda **kw: calls.append(kw))
    return calls


def test_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    calls = _spy_init(monkeypatch)
    sc.configure_sentry()           # no env, no config
    sc.configure_sentry(None)
    assert calls == []


def test_inits_from_config_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    calls = _spy_init(monkeypatch)
    sc.configure_sentry("https://key@errors.example/1")   # from config.toml
    assert len(calls) == 1
    assert calls[0]["dsn"] == "https://key@errors.example/1"
    assert calls[0]["traces_sample_rate"] == 0.0
    assert calls[0]["send_default_pii"] is False


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://env@errors.example/9")
    calls = _spy_init(monkeypatch)
    sc.configure_sentry("https://config@errors.example/1")
    assert len(calls) == 1
    assert calls[0]["dsn"] == "https://env@errors.example/9"  # env wins


def test_idempotent_same_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    calls = _spy_init(monkeypatch)
    sc.configure_sentry("https://key@errors.example/1")
    sc.configure_sentry("https://key@errors.example/1")
    assert len(calls) == 1
