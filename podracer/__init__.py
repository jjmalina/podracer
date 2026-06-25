from importlib.metadata import PackageNotFoundError, version

import structlog

# Key/value-native logger. Prefer structured events with typed fields:
#     logger.info("job_running", job_id=5, episode_id=12, attempt=1)
# rather than pre-formatted message strings. Legacy %-style calls
# (logger.info("msg %s", x)) still render correctly via structlog's
# PositionalArgumentsFormatter, so call sites can be migrated incrementally.
# Output format/handlers are set up in podracer.logging_config.configure_logging.
logger = structlog.get_logger("podracer")

try:
    __version__ = version("podracer")
except PackageNotFoundError:  # running from a raw checkout, not installed
    __version__ = "0.0.0"

# Single source for the outbound User-Agent. Some podcast hosts (e.g. Buzzsprout)
# 403 httpx's default UA, so every outbound fetch (feed.py, download.py) sends
# this app identifier instead. Derived from the package version so it tracks
# pyproject.toml automatically rather than drifting across hardcoded copies.
USER_AGENT = f"podracer/{__version__}"
