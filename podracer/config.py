import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from podracer import logger

CONFIG_FILENAME = "config.toml"
CREDENTIALS_DIR = ".credentials"


def _xdg_config_home() -> Path:
    env = os.environ.get("XDG_CONFIG_HOME")
    return Path(env).expanduser() if env else Path.home() / ".config"


def xdg_config_dir() -> Path:
    """~/.config/podracer/ (or $XDG_CONFIG_HOME/podracer/)."""
    return _xdg_config_home() / "podracer"


@dataclass
class Config:
    db_path: str = "./data/podracer.db"
    media_dir: str = "./data/media/"

    # Transcription
    transcribe_backend: str = "deepgram"
    transcribe_whisperx_model: str = "small"
    transcribe_deepgram_model: str = "nova-3"
    transcribe_device: str = "cuda"
    transcribe_compute_type: str = "float16"
    transcribe_service_url: str | None = None
    transcribe_service_auth_token: str | None = None
    diarize: bool = True

    # Whisper service (server-side, when running `podracer whisper-serve`)
    whisper_service_host: str = "0.0.0.0"
    whisper_service_port: int = 9000
    whisper_service_auth_token: str | None = None

    # Summarization
    summarize_backend: str = "openrouter"
    summarize_model: str = "deepseek/deepseek-v4-flash"
    summarize_base_url: str | None = None

    # Daemon / worker
    sync_interval_minutes: int = 30      # how often to fetch feeds + enqueue
    drain_interval_seconds: int = 10     # how often to check the job queue
    max_attempts: int = 3
    retry_backoff_seconds: int = 300

    # Logging: "auto" (console on a TTY, JSON otherwise) | "console" | "json".
    # The PODRACER_LOG_FORMAT env var overrides this.
    log_format: str = "auto"

    # Error reporting: Sentry/GlitchTip DSN (empty = off). SENTRY_DSN env overrides.
    sentry_dsn: str | None = None

    # API keys
    hf_token: str | None = None
    openrouter_api_key: str | None = None
    deepgram_api_key: str | None = None
    podcast_index_key: str | None = None
    podcast_index_secret: str | None = None

    # Resolved paths (not settable in config)
    _project_root: Path = field(default_factory=Path.cwd, repr=False)


def _read_credential_file(project_root: Path, filename: str) -> str | None:
    path = project_root / CREDENTIALS_DIR / filename
    if path.exists():
        return path.read_text().strip()
    return None


def _read_kv_credential_file(project_root: Path, filename: str) -> dict[str, str]:
    path = project_root / CREDENTIALS_DIR / filename
    if not path.exists():
        return {}
    lines = path.read_text().strip().splitlines()
    result = {}
    for i, line in enumerate(lines):
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
        else:
            result[str(i)] = line.strip()
    return result


def _find_config_file() -> Path | None:
    """Lookup order:
       1. ./config.toml in cwd                  (in-repo dev override)
       2. ~/.config/podracer/config.toml        (XDG / daemon install)
       3. <repo_root>/config.toml via __file__  (editable-install fallback)
    """
    candidates = [
        Path.cwd() / CONFIG_FILENAME,
        xdg_config_dir() / CONFIG_FILENAME,
        Path(__file__).resolve().parent.parent / CONFIG_FILENAME,
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _resolve_path(value: str, base: Path) -> str:
    """Resolve a path string. Absolute paths pass through; relative paths
    are anchored at `base` (the config file's directory)."""
    p = Path(value)
    if p.is_absolute():
        return str(p)
    return str((base / p).resolve())


def load_config() -> Config:
    config = Config()

    config_path = _find_config_file()
    if config_path:
        logger.debug("Loading config from %s", config_path)
        config._project_root = config_path.parent
        with open(config_path, "rb") as f:
            data = tomllib.load(f)

        general = data.get("general", {})
        config.db_path = general.get("db_path", config.db_path)
        config.media_dir = general.get("media_dir", config.media_dir)

        transcribe = data.get("transcribe", {})
        config.transcribe_backend = transcribe.get("backend", config.transcribe_backend)
        config.transcribe_whisperx_model = transcribe.get("whisperx_model", config.transcribe_whisperx_model)
        config.transcribe_deepgram_model = transcribe.get("deepgram_model", config.transcribe_deepgram_model)
        config.transcribe_device = transcribe.get("device", config.transcribe_device)
        config.transcribe_compute_type = transcribe.get("compute_type", config.transcribe_compute_type)
        config.transcribe_service_url = transcribe.get("service_url", config.transcribe_service_url)
        config.transcribe_service_auth_token = transcribe.get(
            "service_auth_token", config.transcribe_service_auth_token,
        )
        config.diarize = transcribe.get("diarize", config.diarize)

        whisper_service = data.get("whisper_service", {})
        config.whisper_service_host = whisper_service.get("host", config.whisper_service_host)
        config.whisper_service_port = whisper_service.get("port", config.whisper_service_port)
        config.whisper_service_auth_token = whisper_service.get(
            "auth_token", config.whisper_service_auth_token,
        )

        summarize = data.get("summarize", {})
        config.summarize_backend = summarize.get("backend", config.summarize_backend)
        config.summarize_model = summarize.get("model", config.summarize_model)
        config.summarize_base_url = summarize.get("base_url", config.summarize_base_url)

        daemon = data.get("daemon", {})
        config.sync_interval_minutes = daemon.get("sync_interval_minutes", config.sync_interval_minutes)
        config.drain_interval_seconds = daemon.get("drain_interval_seconds", config.drain_interval_seconds)
        config.max_attempts = daemon.get("max_attempts", config.max_attempts)
        config.retry_backoff_seconds = daemon.get("retry_backoff_seconds", config.retry_backoff_seconds)

        logging_cfg = data.get("logging", {})
        config.log_format = logging_cfg.get("format", config.log_format)

        sentry = data.get("sentry", {})
        config.sentry_dsn = sentry.get("dsn", config.sentry_dsn)

        keys = data.get("keys", {})
        config.hf_token = keys.get("hf_token")
        config.openrouter_api_key = keys.get("openrouter_api_key")
        config.deepgram_api_key = keys.get("deepgram_api_key")
        config.podcast_index_key = keys.get("podcast_index_key")
        config.podcast_index_secret = keys.get("podcast_index_secret")

    root = config._project_root

    if not config.hf_token:
        config.hf_token = _read_credential_file(root, "hf_token")
    if not config.openrouter_api_key:
        config.openrouter_api_key = _read_credential_file(root, "openrouter_token")
    if not config.deepgram_api_key:
        config.deepgram_api_key = _read_credential_file(root, "deepgram_token")
    if not config.podcast_index_key or not config.podcast_index_secret:
        pi = _read_kv_credential_file(root, "podcast_index")
        if not config.podcast_index_key:
            config.podcast_index_key = pi.get("PODCAST_INDEX_API_KEY") or pi.get("0")
        if not config.podcast_index_secret:
            config.podcast_index_secret = pi.get("PODCAST_INDEX_API_SECRET") or pi.get("1")

    config.db_path = os.environ.get("PODRACER_DB", config.db_path)
    config.media_dir = os.environ.get("PODRACER_MEDIA_DIR", config.media_dir)
    config.hf_token = os.environ.get("HF_TOKEN", config.hf_token)
    config.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", config.openrouter_api_key)
    config.deepgram_api_key = os.environ.get("DEEPGRAM_API_KEY", config.deepgram_api_key)
    config.podcast_index_key = os.environ.get("PODCAST_INDEX_KEY", config.podcast_index_key)
    config.podcast_index_secret = os.environ.get("PODCAST_INDEX_SECRET", config.podcast_index_secret)
    config.log_format = os.environ.get("PODRACER_LOG_FORMAT", config.log_format)
    config.sentry_dsn = os.environ.get("SENTRY_DSN", config.sentry_dsn)

    # Anchor relative db_path / media_dir against the config file's directory.
    # Absolute paths pass through unchanged (deployment uses absolute paths in
    # config.toml or via PODRACER_DB / PODRACER_MEDIA_DIR).
    config.db_path = _resolve_path(config.db_path, config._project_root)
    config.media_dir = _resolve_path(config.media_dir, config._project_root)
    if not config.media_dir.endswith("/"):
        config.media_dir += "/"

    return config
