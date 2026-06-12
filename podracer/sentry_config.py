"""Error reporting to Sentry / GlitchTip.

No-op unless ``SENTRY_DSN`` is set, so local dev and the CLI are unaffected; the
systemd services set it from sops. Capture paths:

* **Web** — the FastAPI integration captures unhandled request exceptions at the
  ASGI layer automatically (no call-site changes).
* **Worker** — it catches-and-logs its exceptions, and our structlog pipeline
  consumes ``exc_info`` before it reaches stdlib log records, so a logging-based
  integration would report stacktrace-less events. The worker therefore captures
  explicitly via ``sentry_sdk.capture_exception()`` (a no-op when Sentry isn't
  initialized).
"""
import os

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

# The DSN we last initialized with: None = never configured, "" = configured as
# off, "<dsn>" = active. Tracking the value (not a bool) lets a later call —
# once config.toml is loaded — turn Sentry on, while no-op'ing redundant calls.
_configured_dsn: str | None = None


def configure_sentry(dsn: str | None = None) -> None:
    """Initialize Sentry. DSN resolution mirrors podracer's config layering: the
    ``SENTRY_DSN`` env var wins, then the value from config.toml (``[sentry] dsn``,
    passed in once config is loaded), else off. Safe to call repeatedly; no-op
    when the resolved DSN is empty or unchanged.
    """
    global _configured_dsn
    resolved = (os.environ.get("SENTRY_DSN") or dsn or "").strip()
    if resolved == _configured_dsn:
        return
    _configured_dsn = resolved
    if not resolved:
        return
    sentry_sdk.init(
        dsn=resolved,
        environment=os.environ.get("PODRACER_ENV", "production"),
        traces_sample_rate=0.0,   # errors only — no performance tracing
        send_default_pii=False,
        # Logs become breadcrumbs but not events: structlog eats exc_info, so
        # log-based event capture would be stacktrace-less. Events come from the
        # FastAPI integration (web) and explicit capture_exception (worker).
        integrations=[LoggingIntegration(event_level=None)],
    )
    _configured = True
