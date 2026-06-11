"""Logging configuration: human-readable on a TTY, JSON for services.

Output format comes from ``config.toml`` (``[logging] format``) or the
``PODRACER_LOG_FORMAT`` env var, which overrides the file (same layering as the
rest of podracer's config):

* ``auto`` (default) — console (pretty) when stderr is a TTY, JSON otherwise.
* ``console`` — force human-readable output.
* ``json`` — force JSON (one object per line).

Auto-detect means an interactive ``podracer <cmd>`` stays readable while the
systemd web/worker services (no TTY) emit JSON for log aggregation, with no
configuration required either way. structlog is bridged to stdlib logging, so
existing ``logging.getLogger("podracer")`` calls render through the same
pipeline; new code can use ``structlog.get_logger(...)`` for typed key/value
fields.
"""
import logging
import os
import sys
from datetime import UTC, datetime

import structlog

# The format we last configured ("json"/"console"), or None if not yet set up.
# Tracking the value (not a bool) lets a later call — once config.toml is loaded
# — switch the format, while no-op'ing redundant calls.
_configured_format: str | None = None


def _add_timestamp(_logger, _method_name, event_dict):
    """Add an RFC3339 timestamp with milliseconds and a numeric UTC offset.

    e.g. ``2026-06-11T02:30:00.123+0000``. This exact shape matters: Fluent Bit
    parses it with ``Time_Format %Y-%m-%dT%H:%M:%S.%L%z``. A bare ``Z`` suffix
    (what most ISO helpers emit) or a missing fractional part fails ``%z``/``%L``,
    and the record silently falls back to ingest time instead of event time.
    """
    now = datetime.now(UTC)
    event_dict["timestamp"] = f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}{now:%z}"
    return event_dict


def _resolve_format(log_format: str | None) -> str:
    """Resolve the effective format, honoring podracer's layering: the
    PODRACER_LOG_FORMAT env var wins, then the value from config.toml (passed in
    by callers once config is loaded), then the built-in default ``auto``."""
    return (os.environ.get("PODRACER_LOG_FORMAT") or log_format or "auto").strip().lower()


def _want_json(resolved: str) -> bool:
    if resolved == "json":
        return True
    if resolved == "console":
        return False
    # auto (or unknown): JSON only when stderr is not a terminal.
    return not sys.stderr.isatty()


def configure_logging(log_format: str | None = None, level: int = logging.INFO) -> None:
    """Configure structlog + stdlib logging for the whole app.

    Safe to call repeatedly: pass ``log_format`` from config.toml once it's
    loaded to switch away from the bootstrap (env/auto) format. Redundant calls
    that resolve to the same format are no-ops.
    """
    global _configured_format
    resolved = _resolve_format(log_format)
    if _configured_format == resolved:
        return

    # Processors shared by structlog-native and foreign (stdlib) log records, so
    # both render identically. merge_contextvars pulls in anything bound via
    # structlog.contextvars (e.g. the worker binds episode_id per job).
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_timestamp,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = structlog.processors.JSONRenderer() if _want_json(resolved) else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured_format = resolved

    if resolved not in ("auto", "console", "json"):
        logging.getLogger("podracer").warning(
            "unknown log format %r (PODRACER_LOG_FORMAT / [logging] format); "
            "falling back to auto-detect", resolved,
        )
