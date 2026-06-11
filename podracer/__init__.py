import structlog

# Key/value-native logger. Prefer structured events with typed fields:
#     logger.info("job_running", job_id=5, episode_id=12, attempt=1)
# rather than pre-formatted message strings. Legacy %-style calls
# (logger.info("msg %s", x)) still render correctly via structlog's
# PositionalArgumentsFormatter, so call sites can be migrated incrementally.
# Output format/handlers are set up in podracer.logging_config.configure_logging.
logger = structlog.get_logger("podracer")
