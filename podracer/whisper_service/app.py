from contextlib import asynccontextmanager

from fastapi import FastAPI

from podracer import logger
from podracer.config import Config
from podracer.logging_config import configure_logging
from podracer.whisper_service.routes import router
from podracer.whisper_service.runner import load_diarize_pipeline, load_whisper_model
from podracer.whisper_service.state import ServiceState


def create_app(cfg: Config) -> FastAPI:
    state = ServiceState(
        model_size=cfg.transcribe_whisperx_model,
        device=cfg.transcribe_device,
        compute_type=cfg.transcribe_compute_type,
        diarize_enabled=cfg.diarize,
        auth_token=cfg.whisper_service_auth_token,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(cfg.log_format)  # consistent format under any ASGI launcher
        state.whisper_model = load_whisper_model(
            state.model_size, state.device, state.compute_type,
        )
        if state.diarize_enabled:
            if cfg.hf_token:
                state.diarize_pipeline = load_diarize_pipeline(cfg.hf_token, state.device)
            else:
                logger.warning("diarize=true but no hf_token configured; diarization disabled")
        app.state.service = state
        logger.info("Whisper service ready (model=%s, device=%s, diarize=%s, auth=%s)",
                    state.model_size, state.device,
                    state.diarize_pipeline is not None,
                    "yes" if state.auth_token else "no")
        yield

    app = FastAPI(title="podracer-whisper", lifespan=lifespan)
    app.include_router(router)
    return app
