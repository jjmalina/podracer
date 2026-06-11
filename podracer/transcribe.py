import argparse
import os
import sys
from pathlib import Path

import httpx
from deepgram import DeepgramClient
from deepgram.core.request_options import RequestOptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from podracer import logger
from podracer.logging_config import configure_logging

DEEPGRAM_TIMEOUT_SECONDS = 1800  # 30 min — large podcast uploads can take a while
WHISPER_SERVICE_TIMEOUT_SECONDS = 3600


def transcribe(
    audio_path: str,
    backend: str = "deepgram",
    model: str = "nova-3",
    hf_token: str | None = None,
    deepgram_api_key: str | None = None,
    diarize: bool = True,
    service_url: str | None = None,
    service_auth_token: str | None = None,
    language: str | None = None,
) -> str:
    """Transcribe an audio file with optional speaker diarization.

    Output format: lines of `[HH:MM:SS] [SPEAKER_XX] text` regardless of backend.

    For local whisperx, run `podracer whisper-serve` and use backend="whisperx-http".
    """
    if backend == "deepgram":
        if not deepgram_api_key:
            raise ValueError("deepgram backend requires deepgram_api_key")
        return _transcribe_deepgram(audio_path, model, deepgram_api_key, diarize)
    if backend == "whisperx-http":
        if not service_url:
            raise ValueError("whisperx-http backend requires service_url")
        return _transcribe_whisperx_http(
            audio_path, service_url, service_auth_token, diarize, language,
        )
    raise ValueError(f"unknown transcribe backend: {backend!r}")


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _transcribe_deepgram(
    audio_path: str,
    model: str,
    api_key: str,
    diarize: bool,
) -> str:
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

    utterances = response.results.utterances or []
    if not utterances:
        if response.results.channels and response.results.channels[0].alternatives:
            text = response.results.channels[0].alternatives[0].transcript or ""
            return f"[00:00:00] [SPEAKER_00] {text}"
        return ""

    lines = []
    for u in utterances:
        start = u.start or 0.0
        speaker_idx = u.speaker if u.speaker is not None else 0
        speaker = f"SPEAKER_{speaker_idx:02d}"
        text = (u.transcript or "").strip()
        if not text:
            continue
        lines.append(f"[{_format_timestamp(start)}] [{speaker}] {text}")
    return "\n".join(lines)


@retry(
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=5, min=5, max=60),
    before_sleep=lambda rs: logger.warning(
        "whisper service request failed, retrying (attempt %d/3)", rs.attempt_number,
    ),
)
def _post_to_whisper_service(
    audio_path: str,
    service_url: str,
    headers: dict,
    data: dict,
    timeout_seconds: int,
) -> httpx.Response:
    filename = os.path.basename(audio_path)
    with open(audio_path, "rb") as f:
        files = {"audio": (filename, f, "application/octet-stream")}
        with httpx.Client(timeout=timeout_seconds) as client:
            return client.post(
                f"{service_url.rstrip('/')}/v1/transcribe",
                headers=headers, files=files, data=data,
            )


def _transcribe_whisperx_http(
    audio_path: str,
    service_url: str,
    auth_token: str | None,
    diarize: bool,
    language: str | None = None,
) -> str:
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    data: dict[str, str] = {"diarize": "true" if diarize else "false"}
    if language:
        data["language"] = language

    logger.info("Uploading to whisper service at %s...", service_url)
    resp = _post_to_whisper_service(
        audio_path, service_url, headers, data, WHISPER_SERVICE_TIMEOUT_SECONDS,
    )
    if not resp.is_success:
        logger.error("whisper service error: %s", resp.text)
    resp.raise_for_status()
    return resp.json()["text"]


def main():
    configure_logging()

    parser = argparse.ArgumentParser(description="Transcribe audio files")
    parser.add_argument("audio_file", help="Path to the audio file to transcribe")
    parser.add_argument("--backend", default="deepgram",
                        choices=["deepgram", "whisperx-http"],
                        help="Transcription backend (default: deepgram)")
    parser.add_argument("--model", default="nova-3",
                        help="Model name for deepgram (default: nova-3)")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--deepgram-key", help="Deepgram API key (or DEEPGRAM_API_KEY env)")
    parser.add_argument("--service-url", help="Whisper service URL (whisperx-http only)")
    parser.add_argument("--service-token", help="Bearer token for whisper service")
    parser.add_argument("--no-diarize", action="store_true", help="Skip speaker diarization")

    args = parser.parse_args()

    if not Path(args.audio_file).exists():
        logger.error("File not found: %s", args.audio_file)
        sys.exit(1)

    diarize = not args.no_diarize

    if args.backend == "deepgram":
        api_key = args.deepgram_key or os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            logger.error("Deepgram backend requires --deepgram-key or DEEPGRAM_API_KEY")
            sys.exit(1)
        text = transcribe(args.audio_file, backend="deepgram", model=args.model,
                          deepgram_api_key=api_key, diarize=diarize)
    else:
        service_url = args.service_url or os.environ.get("PODRACER_WHISPER_SERVICE_URL")
        if not service_url:
            logger.error("whisperx-http requires --service-url or PODRACER_WHISPER_SERVICE_URL")
            sys.exit(1)
        text = transcribe(args.audio_file, backend="whisperx-http",
                          service_url=service_url,
                          service_auth_token=args.service_token,
                          diarize=diarize)

    if args.output:
        Path(args.output).write_text(text)
        logger.info("Transcription saved to: %s", args.output)
    else:
        print(text)


if __name__ == "__main__":
    main()
