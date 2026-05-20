"""Whisperx model loading and transcription. This is the only module that imports
torch / whisperx. The rest of podracer talks to the service over HTTP.
"""
import torch

# PyTorch 2.6+ defaults weights_only=True; pyannote/lightning needs it False.
# Patch torch.load BEFORE importing whisperx (which transitively loads pyannote).
_original_torch_load = torch.load


def _patched_torch_load(*args, **kwargs):
    kwargs["weights_only"] = False
    return _original_torch_load(*args, **kwargs)


torch.load = _patched_torch_load

import whisperx  # noqa: E402
from whisperx.diarize import DiarizationPipeline  # noqa: E402

from podracer import logger  # noqa: E402


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_whisper_model(model_size: str, device: str, compute_type: str):
    logger.info("Loading whisper model: %s (device=%s, compute=%s)", model_size, device, compute_type)
    return whisperx.load_model(model_size, device=device, compute_type=compute_type)


def load_align_model(language: str, device: str):
    logger.info("Loading alignment model for language: %s", language)
    return whisperx.load_align_model(language_code=language, device=device)


def load_diarize_pipeline(hf_token: str, device: str):
    logger.info("Loading diarization pipeline")
    return DiarizationPipeline(token=hf_token, device=device)


def transcribe_audio(
    audio_path: str,
    whisper_model,
    device: str,
    diarize_pipeline=None,
    align_cache: dict | None = None,
    language: str | None = None,
) -> tuple[str, str]:
    """Run transcription using pre-loaded models. Returns formatted transcript.

    `align_cache` is a mutable dict keyed by language code that holds (model, metadata)
    tuples so alignment models load once per language across requests.
    """
    audio = whisperx.load_audio(audio_path)
    result = whisper_model.transcribe(audio, language=language) if language \
        else whisper_model.transcribe(audio)
    detected_language = result["language"]
    logger.info("Detected language: %s", detected_language)

    if align_cache is not None and detected_language in align_cache:
        align_model, metadata = align_cache[detected_language]
    else:
        align_model, metadata = load_align_model(detected_language, device)
        if align_cache is not None:
            align_cache[detected_language] = (align_model, metadata)

    result = whisperx.align(
        result["segments"], align_model, metadata, audio, device, return_char_alignments=False
    )

    if diarize_pipeline is not None:
        logger.info("Running speaker diarization...")
        diarize_segments = diarize_pipeline(audio_path)
        result = whisperx.assign_word_speakers(diarize_segments, result)

    lines = []
    for segment in result["segments"]:
        speaker = segment.get("speaker", "SPEAKER")
        start = segment["start"]
        text = segment["text"]
        lines.append(f"[{_format_timestamp(start)}] [{speaker}] {text}")

    return "\n".join(lines), detected_language
