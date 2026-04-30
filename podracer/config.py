import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from podracer import logger

CONFIG_FILENAME = "config.toml"
CREDENTIALS_DIR = ".credentials"


@dataclass
class Config:
    db_path: str = "./data/podracer.db"
    media_dir: str = "./data/media/"

    # Transcription
    transcribe_model: str = "small"
    transcribe_device: str = "cuda"
    transcribe_compute_type: str = "float16"
    diarize: bool = True

    # Summarization
    summarize_backend: str = "ollama"
    summarize_model: str = "gemma4:e4b"
    summarize_base_url: str | None = None

    # API keys
    hf_token: str | None = None
    openrouter_api_key: str | None = None
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
    for base in [Path.cwd(), Path(__file__).resolve().parent.parent]:
        path = base / CONFIG_FILENAME
        if path.exists():
            return path
    return None


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
        config.transcribe_model = transcribe.get("model", config.transcribe_model)
        config.transcribe_device = transcribe.get("device", config.transcribe_device)
        config.transcribe_compute_type = transcribe.get("compute_type", config.transcribe_compute_type)
        config.diarize = transcribe.get("diarize", config.diarize)

        summarize = data.get("summarize", {})
        config.summarize_backend = summarize.get("backend", config.summarize_backend)
        config.summarize_model = summarize.get("model", config.summarize_model)
        config.summarize_base_url = summarize.get("base_url", config.summarize_base_url)

        keys = data.get("keys", {})
        config.hf_token = keys.get("hf_token")
        config.openrouter_api_key = keys.get("openrouter_api_key")
        config.podcast_index_key = keys.get("podcast_index_key")
        config.podcast_index_secret = keys.get("podcast_index_secret")

    root = config._project_root

    if not config.hf_token:
        config.hf_token = _read_credential_file(root, "hf_token")
    if not config.openrouter_api_key:
        config.openrouter_api_key = _read_credential_file(root, "openrouter_token")
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
    config.podcast_index_key = os.environ.get("PODCAST_INDEX_KEY", config.podcast_index_key)
    config.podcast_index_secret = os.environ.get("PODCAST_INDEX_SECRET", config.podcast_index_secret)

    return config
