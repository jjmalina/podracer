import argparse
import json
import os
import sys
from pathlib import Path

from podracer.summarize import Backend, PodcastSummary, summarize


def print_summary(result: PodcastSummary) -> None:
    print("=" * 60)
    print("SPEAKERS")
    print("=" * 60)
    for s in result.speakers:
        print(f"  {s.label} = {s.name} ({s.role})")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(result.summary)

    print("\n" + "=" * 60)
    print("CHAPTERS")
    print("=" * 60)
    for ch in result.chapters:
        print(f"\n[{ch.timestamp}] {ch.title}")
        print(f"  {ch.summary}")

    print("\n" + "=" * 60)
    print("INSIGHTS")
    print("=" * 60)
    for ins in result.insights:
        print(f"\n[{ins.timestamp}] ({ins.speaker})")
        print(f"  {ins.text}")

    print("\n" + "=" * 60)
    print("SPEAKER TAKES")
    print("=" * 60)
    for take in result.speaker_takes:
        print(f"\n[{take.timestamp}] {take.speaker}:")
        print(f"  {take.take}")


def main():
    parser = argparse.ArgumentParser(description="Summarize a podcast transcript")
    parser.add_argument("transcript_file", help="Path to a transcript text file")
    parser.add_argument("--model", default="gemma4:e4b", help="Model name (default: gemma4:e4b)")
    parser.add_argument("--backend", choices=["ollama", "vllm", "openrouter"], default="ollama", help="Inference backend (default: ollama)")
    parser.add_argument("--base-url", default=None, help="Backend API base URL (default: auto per backend)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted text")

    args = parser.parse_args()

    path = Path(args.transcript_file)
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        sys.exit(1)

    if args.backend == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            print("Error: OPENROUTER_API_KEY environment variable is required", file=sys.stderr)
            sys.exit(1)
        backend = Backend.openrouter(args.model, api_key)
    elif args.backend == "vllm":
        backend = Backend.vllm(args.model, args.base_url or "http://localhost:8000")
    else:
        backend = Backend.ollama(args.model, args.base_url or "http://localhost:11434")

    transcript = path.read_bytes().decode("utf-8")
    result = summarize(transcript, backend=backend)

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print_summary(result)


if __name__ == "__main__":
    main()
