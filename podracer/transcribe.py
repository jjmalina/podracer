import argparse
import os
import sys
from pathlib import Path

from podracer import logger


def load_hf_token() -> str | None:
    """Load HF token from env var or credentials file."""
    if token := os.environ.get("HF_TOKEN"):
        return token

    cred_file = Path(__file__).parent.parent / ".credentials" / "hf_token"
    if cred_file.exists():
        return cred_file.read_text().strip()

    return None


def transcribe(
    audio_path: str,
    backend: str = "whisperx",
    model: str = "small",
    device: str = "cuda",
    compute_type: str = "float16",
    hf_token: str | None = None,
    deepgram_api_key: str | None = None,
    diarize: bool = True,
) -> str:
    """Transcribe an audio file with optional speaker diarization.

    Output format: lines of `[HH:MM:SS] [SPEAKER_XX] text` regardless of backend.
    """
    if backend == "deepgram":
        if not deepgram_api_key:
            raise ValueError("deepgram backend requires deepgram_api_key")
        return _transcribe_deepgram(audio_path, model, deepgram_api_key, diarize)
    return _transcribe_whisperx(audio_path, model, device, compute_type,
                                hf_token if diarize else None)


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


DEEPGRAM_TIMEOUT_SECONDS = 1800  # 30 min — large podcast uploads can take a while


def _transcribe_deepgram(
    audio_path: str,
    model: str,
    api_key: str,
    diarize: bool,
) -> str:
    from deepgram import DeepgramClient
    from deepgram.core.request_options import RequestOptions

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
        # Fallback: pull the channel-level transcript if utterances weren't returned.
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


def _transcribe_whisperx(
    audio_path: str,
    model_size: str,
    device: str,
    compute_type: str,
    hf_token: str | None,
) -> str:
    # Lazy torch/whisperx import so non-whisperx callers don't pay the cost
    # and CPU-only deployments don't need torch installed at all.
    import torch

    # Fix for PyTorch 2.6+ weights_only default change.
    # pyannote models use omegaconf which isn't in the safe globals list.
    # Lightning explicitly passes weights_only=True, so we force it to False.
    _original_torch_load = torch.load
    def _patched_torch_load(*args, **kwargs):
        kwargs["weights_only"] = False
        return _original_torch_load(*args, **kwargs)
    torch.load = _patched_torch_load

    import whisperx
    from whisperx.diarize import DiarizationPipeline

    model = whisperx.load_model(model_size, device=device, compute_type=compute_type)
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio)

    logger.info("Detected language: %s", result["language"])

    align_model, metadata = whisperx.load_align_model(
        language_code=result["language"], device=device
    )
    result = whisperx.align(
        result["segments"], align_model, metadata, audio, device, return_char_alignments=False
    )

    if hf_token:
        logger.info("Running speaker diarization...")
        diarize_model = DiarizationPipeline(token=hf_token, device=device)
        diarize_segments = diarize_model(audio_path)
        result = whisperx.assign_word_speakers(diarize_segments, result)

    full_text = []
    for segment in result["segments"]:
        speaker = segment.get("speaker", "SPEAKER")
        start = segment["start"]
        end = segment["end"]
        text = segment["text"]
        logger.debug("[%.2fs -> %.2fs] [%s] %s", start, end, speaker, text)
        full_text.append(f"[{_format_timestamp(start)}] [{speaker}] {text}")

    return "\n".join(full_text)


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio files")
    parser.add_argument("audio_file", help="Path to the audio file to transcribe")
    parser.add_argument("--backend", default="whisperx", choices=["whisperx", "deepgram"],
                        help="Transcription backend (default: whisperx)")
    parser.add_argument("--model", default=None,
                        help="Model name (whisperx: small/medium/large; deepgram: nova-3)")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                        help="Device (whisperx only, default: cuda)")
    parser.add_argument("--compute-type", default="float16",
                        help="Compute type (whisperx only, default: float16, use int8 for CPU)")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--hf-token", help="HuggingFace token (whisperx diarization)")
    parser.add_argument("--deepgram-key", help="Deepgram API key (or set DEEPGRAM_API_KEY)")
    parser.add_argument("--no-diarize", action="store_true", help="Skip speaker diarization")

    args = parser.parse_args()

    if not Path(args.audio_file).exists():
        logger.error("File not found: %s", args.audio_file)
        sys.exit(1)

    diarize = not args.no_diarize
    backend = args.backend
    default_model = "nova-3" if backend == "deepgram" else "small"
    model = args.model or default_model

    if backend == "deepgram":
        api_key = args.deepgram_key or os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            logger.error("Deepgram backend requires --deepgram-key or DEEPGRAM_API_KEY")
            sys.exit(1)
        text = transcribe(args.audio_file, backend="deepgram", model=model,
                          deepgram_api_key=api_key, diarize=diarize)
    else:
        hf_token = (args.hf_token or load_hf_token()) if diarize else None
        if diarize and not hf_token:
            logger.info("No HF_TOKEN provided, skipping speaker diarization")
        text = transcribe(args.audio_file, backend="whisperx", model=model,
                          device=args.device, compute_type=args.compute_type,
                          hf_token=hf_token, diarize=diarize)

    if args.output:
        Path(args.output).write_text(text)
        logger.info("Transcription saved to: %s", args.output)
    else:
        print(text)


if __name__ == "__main__":
    main()
