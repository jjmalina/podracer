"""Transcription backends.

Every backend produces lines of `[HH:MM:SS] [SPEAKER_XX] text`. Callers go
through `transcribe(audio_path, cfg)`, which picks and validates the backend
from config and returns the text together with the engine:model that actually
produced it (stored as `transcripts.model`).

Backends (`[transcribe] backend`):

* ``deepgram`` — Deepgram cloud API.
* ``whisperx-http`` — the self-hosted whisper service (`podracer.whisper_service`)
  only; retries connection errors, then fails the job.
* ``prefer-whisperx`` — use the whisper service when it answers its health
  check, otherwise Deepgram. A service that dies mid-request (host powered off
  or asleep) also falls back to Deepgram rather than failing the job.
"""
import argparse
import os
import socket
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import sentry_sdk
from deepgram import DeepgramClient
from deepgram.core.request_options import RequestOptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from podracer import logger
from podracer.config import Config, load_config
from podracer.logging_config import configure_logging
from podracer.timestamps import format_timestamp

DEEPGRAM_TIMEOUT_SECONDS = 1800  # 30 min — large podcast uploads can take a while
WHISPER_SERVICE_TIMEOUT_SECONDS = 3600
# The health probe decides where each job goes, so it must fail fast: a host
# that is asleep or powered off doesn't refuse the connection, it just never
# answers.
WHISPER_HEALTH_TIMEOUT_SECONDS = 3

BACKENDS = ("deepgram", "whisperx-http", "prefer-whisperx")

# The service sends nothing until the transcript is done (minutes), so a read
# timeout can't tell "busy" from "host vanished". TCP keepalive can: a peer that
# lost power stops ACKing probes and the request fails in ~idle+intvl*cnt
# seconds instead of hanging for the full WHISPER_SERVICE_TIMEOUT_SECONDS.
_KEEPALIVE_OPTS: list[tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
for _name, _value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
    if hasattr(socket, _name):  # Linux; macOS lacks some of these
        _KEEPALIVE_OPTS.append((socket.IPPROTO_TCP, getattr(socket, _name), _value))


class TranscribeConfigError(RuntimeError):
    """The configured backend is missing something it needs (key, URL)."""


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    source: str  # "<engine>:<model>", e.g. "deepgram:nova-3", "whisperx:large-v3"


def transcribe(
    audio_path: str,
    cfg: Config,
    *,
    backend: str | None = None,
    deepgram_model: str | None = None,
    diarize: bool | None = None,
) -> TranscriptResult:
    """Transcribe `audio_path` with the configured backend (or `backend`).

    Raises TranscribeConfigError when the backend lacks its credentials/URL.
    """
    backend = backend or cfg.transcribe_backend
    model = deepgram_model or cfg.transcribe_deepgram_model
    diarize = cfg.diarize if diarize is None else diarize

    if backend not in BACKENDS:
        raise TranscribeConfigError(
            f"unknown transcribe backend: {backend!r} (expected one of {', '.join(BACKENDS)})",
        )
    if backend in ("deepgram", "prefer-whisperx") and not cfg.deepgram_api_key:
        raise TranscribeConfigError(f"{backend} backend requires DEEPGRAM_API_KEY")
    if backend in ("whisperx-http", "prefer-whisperx") and not cfg.transcribe_service_url:
        raise TranscribeConfigError(f"{backend} backend requires transcribe.service_url")

    if backend == "deepgram":
        return _transcribe_deepgram(audio_path, model, _require(cfg.deepgram_api_key), diarize)

    service_url = _require(cfg.transcribe_service_url)
    token = cfg.transcribe_service_auth_token
    if backend == "whisperx-http":
        return _transcribe_whisperx_http(audio_path, service_url, token, diarize, retries=True)

    # prefer-whisperx
    unavailable = whisper_service_unavailable(service_url, diarize)
    if unavailable is None:
        try:
            result = _transcribe_whisperx_http(audio_path, service_url, token, diarize,
                                               retries=False)
            logger.info("transcribe_backend_selected", backend="whisperx", source=result.source)
            return result
        except (httpx.HTTPError, ValueError, KeyError) as e:
            unavailable = f"request failed: {e!r}"
            # A rejected request (bad token, missing diarization) is a config
            # problem that would silently route every job to the paid API, so
            # report it; an unreachable host is the expected desktop-off case.
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                logger.error("whisper_service_rejected_request",
                             status=e.response.status_code, body=e.response.text[:500])
                sentry_sdk.capture_message(
                    f"whisper service rejected request ({e.response.status_code}); "
                    "falling back to Deepgram", level="error",
                )
    logger.info("transcribe_backend_selected", backend="deepgram",
                fallback_reason=unavailable)
    return _transcribe_deepgram(audio_path, model, _require(cfg.deepgram_api_key), diarize)


def _require(value: str | None) -> str:
    assert value, "validated in transcribe()"
    return value


def whisper_service_unavailable(service_url: str, diarize: bool) -> str | None:
    """Probe the whisper service. Returns None if it can take the job, else why not."""
    try:
        resp = httpx.get(f"{service_url.rstrip('/')}/v1/health",
                         timeout=WHISPER_HEALTH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        health = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return f"health check failed: {e!r}"
    if health.get("status") != "ok":
        return f"health status {health.get('status')!r}"
    if diarize and not health.get("diarize_available"):
        return "diarization requested but service has no diarization pipeline"
    return None


def _transcribe_deepgram(
    audio_path: str,
    model: str,
    api_key: str,
    diarize: bool,
) -> TranscriptResult:
    logger.info("Transcribing via Deepgram (%s)...", model)
    client = DeepgramClient(api_key=api_key)
    with open(audio_path, "rb") as f:
        audio = f.read()

    response = client.listen.v1.media.transcribe_file(
        request=audio,
        model=model,
        diarize=diarize,
        utterances=True,
        punctuate=True,
        smart_format=True,
        request_options=RequestOptions(timeout_in_seconds=DEEPGRAM_TIMEOUT_SECONDS),
    )
    source = f"deepgram:{model}"

    utterances = response.results.utterances or []
    if not utterances:
        if response.results.channels and response.results.channels[0].alternatives:
            text = response.results.channels[0].alternatives[0].transcript or ""
            return TranscriptResult(f"[00:00:00] [SPEAKER_00] {text}", source)
        return TranscriptResult("", source)

    lines = []
    for u in utterances:
        start = u.start or 0.0
        speaker_idx = u.speaker if u.speaker is not None else 0
        speaker = f"SPEAKER_{speaker_idx:02d}"
        text = (u.transcript or "").strip()
        if not text:
            continue
        lines.append(f"[{format_timestamp(start)}] [{speaker}] {text}")
    return TranscriptResult("\n".join(lines), source)


def _post_to_whisper_service(
    audio_path: str,
    service_url: str,
    headers: dict,
    data: dict,
    timeout_seconds: int,
) -> httpx.Response:
    filename = os.path.basename(audio_path)
    transport = httpx.HTTPTransport(socket_options=_KEEPALIVE_OPTS)
    with open(audio_path, "rb") as f:
        files = {"audio": (filename, f, "application/octet-stream")}
        with httpx.Client(timeout=timeout_seconds, transport=transport) as client:
            return client.post(
                f"{service_url.rstrip('/')}/v1/transcribe",
                headers=headers, files=files, data=data,
            )


_post_with_retries = retry(
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=5, min=5, max=60),
    before_sleep=lambda rs: logger.warning(
        "whisper service request failed, retrying (attempt %d/3)", rs.attempt_number,
    ),
)(_post_to_whisper_service)


def _transcribe_whisperx_http(
    audio_path: str,
    service_url: str,
    auth_token: str | None,
    diarize: bool,
    *,
    retries: bool,
    language: str | None = None,
) -> TranscriptResult:
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    data: dict[str, str] = {"diarize": "true" if diarize else "false"}
    if language:
        data["language"] = language

    logger.info("Uploading to whisper service at %s...", service_url)
    post = _post_with_retries if retries else _post_to_whisper_service
    resp = post(audio_path, service_url, headers, data, WHISPER_SERVICE_TIMEOUT_SECONDS)
    if not resp.is_success:
        logger.error("whisper service error: %s", resp.text)
    resp.raise_for_status()
    body = resp.json()
    # Label with the model the service actually ran, not the client's config.
    return TranscriptResult(body["text"], f"whisperx:{body.get('model') or 'unknown'}")


def main():
    configure_logging()

    parser = argparse.ArgumentParser(description="Transcribe audio files")
    parser.add_argument("audio_file", help="Path to the audio file to transcribe")
    parser.add_argument("--backend", default=None, choices=BACKENDS,
                        help="Transcription backend (default: from config)")
    parser.add_argument("--model", default=None,
                        help="Model name for deepgram (default: from config)")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--deepgram-key", help="Deepgram API key (default: from config/env)")
    parser.add_argument("--service-url", help="Whisper service URL (default: from config)")
    parser.add_argument("--service-token", help="Bearer token for whisper service")
    parser.add_argument("--no-diarize", action="store_true", help="Skip speaker diarization")

    args = parser.parse_args()

    if not Path(args.audio_file).exists():
        logger.error("File not found: %s", args.audio_file)
        sys.exit(1)

    cfg = load_config()
    cfg = replace(
        cfg,
        deepgram_api_key=args.deepgram_key or cfg.deepgram_api_key,
        transcribe_service_url=args.service_url or cfg.transcribe_service_url,
        transcribe_service_auth_token=args.service_token or cfg.transcribe_service_auth_token,
    )
    try:
        result = transcribe(args.audio_file, cfg, backend=args.backend,
                            deepgram_model=args.model, diarize=not args.no_diarize)
    except TranscribeConfigError as e:
        logger.error("%s", e)
        sys.exit(1)

    if args.output:
        Path(args.output).write_text(result.text)
        logger.info("Transcription saved to: %s (%s)", args.output, result.source)
    else:
        print(result.text)


if __name__ == "__main__":
    main()
