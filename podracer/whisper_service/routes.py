import asyncio
import hmac
import os
import tempfile
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile

from podracer import logger
from podracer.whisper_service.runner import transcribe_audio
from podracer.whisper_service.state import ServiceState

router = APIRouter()

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def get_state(request: Request) -> ServiceState:
    return request.app.state.service


def check_auth(state: ServiceState, authorization: str | None) -> None:
    if state.auth_token is None:
        return
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("rejected request with missing/malformed Authorization header")
        raise HTTPException(status_code=401, detail={"error": "missing_auth"})
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, state.auth_token):
        logger.warning("rejected request with invalid auth token")
        raise HTTPException(status_code=403, detail={"error": "invalid_token"})


async def _stream_to_tempfile(upload: UploadFile) -> str:
    suffix = os.path.splitext(upload.filename or "audio")[1] or ".bin"
    fd, path = tempfile.mkstemp(prefix="whisper-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


@router.post("/v1/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    diarize: str = Form("true"),
    language: str | None = Form(None),
    authorization: str | None = Header(None),
    state: ServiceState = Depends(get_state),
):
    check_auth(state, authorization)

    diarize_bool = diarize.lower() in ("true", "1", "yes")
    if diarize_bool and state.diarize_pipeline is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "diarization_unavailable",
                    "message": "service started without HF token"},
        )

    audio_path = await _stream_to_tempfile(audio)
    start = time.time()
    try:
        async with state.lock:
            try:
                text, detected_language = await asyncio.to_thread(
                    transcribe_audio,
                    audio_path,
                    state.whisper_model,
                    state.device,
                    state.diarize_pipeline if diarize_bool else None,
                    state.align_cache,
                    language,
                )
            except Exception as e:
                state.requests_failed += 1
                state.last_error = repr(e)
                logger.exception("transcription failed")
                raise HTTPException(status_code=500, detail={"error": "internal",
                                                              "message": str(e)})

        elapsed = time.time() - start
        state.requests_total += 1
        state.last_request_at = datetime.now(UTC).isoformat()
        return {
            "text": text,
            "language": detected_language,
            "model": state.model_size,
            "diarized": diarize_bool,
            "elapsed_seconds": round(elapsed, 2),
        }
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass


@router.get("/v1/health")
def health(state: ServiceState = Depends(get_state)):
    return {
        "status": "ok",
        "model": state.model_size,
        "device": state.device,
        "compute_type": state.compute_type,
        "diarize_available": state.diarize_pipeline is not None,
        "in_flight": 1 if state.lock.locked() else 0,
    }


@router.get("/v1/info")
def info(state: ServiceState = Depends(get_state)):
    return {
        "model": state.model_size,
        "requests_total": state.requests_total,
        "requests_failed": state.requests_failed,
        "last_error": state.last_error,
        "last_request_at": state.last_request_at,
        "in_flight": 1 if state.lock.locked() else 0,
        "align_languages_loaded": list(state.align_cache.keys()),
    }
