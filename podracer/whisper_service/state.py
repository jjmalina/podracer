import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServiceState:
    model_size: str
    device: str
    compute_type: str
    diarize_enabled: bool
    auth_token: str | None
    whisper_model: Any = None
    diarize_pipeline: Any = None
    align_cache: dict[str, tuple] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    requests_total: int = 0
    requests_failed: int = 0
    last_error: str | None = None
    last_request_at: str | None = None
