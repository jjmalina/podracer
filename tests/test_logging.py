import io
import json
import re
import sys

import structlog

from podracer import logging_config


def test_resolve_format_env_wins(monkeypatch):
    monkeypatch.setenv("PODRACER_LOG_FORMAT", "json")
    # env overrides the value from config.toml
    assert logging_config._resolve_format("console") == "json"


def test_resolve_format_falls_back_to_config_then_default(monkeypatch):
    monkeypatch.delenv("PODRACER_LOG_FORMAT", raising=False)
    assert logging_config._resolve_format("console") == "console"  # from config.toml
    assert logging_config._resolve_format(None) == "auto"          # built-in default


def test_want_json_auto_uses_tty(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    assert logging_config._want_json("auto") is False  # TTY -> console
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    assert logging_config._want_json("auto") is True   # non-TTY -> JSON
    assert logging_config._want_json("json") is True
    assert logging_config._want_json("console") is False


def test_timestamp_format_is_fluentbit_parseable():
    event = logging_config._add_timestamp(None, None, {})
    # RFC3339 with milliseconds and a NUMERIC offset (not 'Z'), so Fluent Bit's
    # Time_Format %Y-%m-%dT%H:%M:%S.%L%z matches it.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}", event["timestamp"])


def test_json_output_has_typed_fields(monkeypatch):
    """A kv event renders as JSON with numbers kept numeric (not stringified)."""
    monkeypatch.setenv("PODRACER_LOG_FORMAT", "json")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    monkeypatch.setattr(logging_config, "_configured_format", None)
    structlog.reset_defaults()

    logging_config.configure_logging()
    log = structlog.get_logger("podracer.test")
    log.info("llm_call", backend="openrouter", model="deepseek/x", input_tokens=512, output_tokens=128)

    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["event"] == "llm_call"
    assert rec["level"] == "info"
    assert rec["model"] == "deepseek/x"
    # The whole point: numbers stay numbers so OpenSearch aggregations work.
    assert rec["input_tokens"] == 512 and isinstance(rec["input_tokens"], int)
    assert rec["output_tokens"] == 128 and isinstance(rec["output_tokens"], int)


def test_config_format_applies_when_env_unset(monkeypatch):
    """With no env var, a config.toml value of 'json' produces JSON output."""
    monkeypatch.delenv("PODRACER_LOG_FORMAT", raising=False)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    monkeypatch.setattr(logging_config, "_configured_format", None)
    structlog.reset_defaults()

    logging_config.configure_logging(log_format="json")  # as if from config.toml
    structlog.get_logger("podracer.test").info("hello", k=1)

    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["event"] == "hello" and rec["k"] == 1
