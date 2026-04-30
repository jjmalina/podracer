import argparse
import os
import sys
from pathlib import Path

# Fix for PyTorch 2.6+ weights_only default change
# pyannote models use omegaconf which isn't in the safe globals list
# Lightning explicitly passes weights_only=True, so we force it to False
import torch

from podracer import logger

_original_torch_load = torch.load
def _patched_torch_load(*args, **kwargs):
    kwargs["weights_only"] = False
    return _original_torch_load(*args, **kwargs)
torch.load = _patched_torch_load  # type: ignore[assignment]

import whisperx
from whisperx.diarize import DiarizationPipeline


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
    model_size: str = "small",
    device: str = "cuda",
    compute_type: str = "float16",
    hf_token: str | None = None,
) -> str:
    """Transcribe an audio file with optional speaker diarization."""
    # Load model and transcribe
    model = whisperx.load_model(model_size, device=device, compute_type=compute_type)
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio)

    logger.info("Detected language: %s", result["language"])

    # Align whisper output for better timestamps
    align_model, metadata = whisperx.load_align_model(
        language_code=result["language"], device=device
    )
    result = whisperx.align(
        result["segments"], align_model, metadata, audio, device, return_char_alignments=False
    )

    # Diarization (if HF token provided)
    if hf_token:
        logger.info("Running speaker diarization...")
        diarize_model = DiarizationPipeline(token=hf_token, device=device)
        diarize_segments = diarize_model(audio_path)
        result = whisperx.assign_word_speakers(diarize_segments, result)

    # Output results
    full_text = []
    for segment in result["segments"]:
        speaker = segment.get("speaker", "SPEAKER")
        start = segment["start"]
        end = segment["end"]
        text = segment["text"]
        logger.debug("[%.2fs -> %.2fs] [%s] %s", start, end, speaker, text)
        minutes, secs = divmod(int(start), 60)
        hours, minutes = divmod(minutes, 60)
        ts = f"{hours:02d}:{minutes:02d}:{secs:02d}"
        full_text.append(f"[{ts}] [{speaker}] {text}")

    return "\n".join(full_text)


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio files with optional diarization")
    parser.add_argument("audio_file", help="Path to the audio file to transcribe")
    parser.add_argument("--model", default="small", help="Whisper model size (default: small)")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"], help="Device to use (default: cuda)")
    parser.add_argument("--compute-type", default="float16", help="Compute type (default: float16, use int8 for CPU)")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--hf-token", help="HuggingFace token for diarization (or set HF_TOKEN env var)")
    parser.add_argument("--no-diarize", action="store_true", help="Skip speaker diarization even if token is available")

    args = parser.parse_args()

    if not Path(args.audio_file).exists():
        logger.error("File not found: %s", args.audio_file)
        sys.exit(1)

    hf_token = None if args.no_diarize else (args.hf_token or load_hf_token())
    if not hf_token:
        logger.info("No HF_TOKEN provided, skipping speaker diarization")

    text = transcribe(args.audio_file, args.model, args.device, args.compute_type, hf_token)

    if args.output:
        Path(args.output).write_text(text)
        logger.info("Transcription saved to: %s", args.output)
    else:
        print(text)


if __name__ == "__main__":
    main()
