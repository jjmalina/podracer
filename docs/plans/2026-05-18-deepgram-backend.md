# Deepgram transcription backend

## Goal

Add Deepgram as an alternative transcription backend so podracer can run without a local GPU. Keep WhisperX as the default; make the choice configurable per-run.

## Design

Mirror the summarization backend pattern: config + CLI flag, with a thin abstraction layer so each backend produces the same output format.

### Output contract

All backends return a single string in the existing format the summarizer already consumes:

```
[HH:MM:SS] [SPEAKER_XX] text
[HH:MM:SS] [SPEAKER_XX] text
...
```

Segment-level granularity. Speaker labels are `SPEAKER_00`, `SPEAKER_01`, etc. — matching whisperx's diarize output so the speaker-ID prompt in `summarize.py` keeps working unchanged.

### Files to change

1. **`podracer/transcribe.py`**
   - Refactor `transcribe()` into a dispatcher that picks the backend.
   - Move existing whisperx logic into `_transcribe_whisperx()`.
   - Add `_transcribe_deepgram(audio_path, model, api_key, diarize)` that calls Deepgram's prerecorded API and formats the response into the standard output.
   - Lazy-import `whisperx`/`torch` inside `_transcribe_whisperx` so deepgram-only users don't pay the import cost or need GPU deps.

2. **`podracer/config.py`**
   - Add `transcribe_backend: str = "whisperx"` (allowed: `whisperx`, `deepgram`).
   - Add `deepgram_api_key: str | None = None` loaded from `[keys]`, `.credentials/deepgram_token`, or `DEEPGRAM_API_KEY` env var.
   - `transcribe_model` already exists; for deepgram, treat the value as the Deepgram model name (e.g. `nova-3`).
   - Make `transcribe_device` / `transcribe_compute_type` ignored for deepgram (no warning, just unused).

3. **`config.toml`**
   - Add commented examples for `transcribe.backend = "deepgram"` and the key.

4. **`podracer/cli.py`**
   - `cmd_transcribe` and `cmd_process`: add `--backend` flag (`whisperx` | `deepgram`), pass through to `transcribe()`.
   - Wire `cfg.deepgram_api_key` and `cfg.transcribe_backend` into the call.
   - Validate: if backend is deepgram, error if no API key.

5. **`pyproject.toml`**
   - Add `deepgram-sdk>=3.0` as an optional dep? Or required? Recommend required since it's pure-Python and tiny. Default to required.

6. **`podracer/db.py`**
   - The `transcripts` table has a `model` column. Store backend+model as `"deepgram:nova-3"` or `"whisperx:small"` so we can tell which backend produced a transcript later. Update `save_transcript` callers in cli.py to pass this composite string.

### Deepgram API call

Use the official SDK:

```python
from deepgram import DeepgramClient, PrerecordedOptions

def _transcribe_deepgram(audio_path: str, model: str, api_key: str, diarize: bool) -> str:
    client = DeepgramClient(api_key)
    with open(audio_path, "rb") as f:
        source = {"buffer": f.read()}
    options = PrerecordedOptions(
        model=model,
        diarize=diarize,
        punctuate=True,
        utterances=True,
        smart_format=True,
    )
    response = client.listen.prerecorded.v("1").transcribe_file(source, options)
    return _format_deepgram(response)
```

`utterances=True` is the key — it returns paragraph-like chunks with `speaker`, `start`, `end`, `transcript` fields. Format each utterance as one line in our standard format.

Speaker normalization: Deepgram returns integer speaker IDs (`0`, `1`, ...). Map them to `SPEAKER_00`, `SPEAKER_01` to match whisperx's labels.

### CLI behavior

```bash
# Use config default (whisperx)
podracer transcribe 1023

# Override at command line
podracer transcribe 1023 --backend deepgram --model nova-3

# Process command also gets the flag
podracer process 1023 --backend deepgram
```

## Open questions

- **Audio upload size**: Deepgram's prerecorded endpoint accepts files up to a few GB but billing meters by duration not size. A 3-hour Huberman episode at 128 MB should be fine. No chunking needed.
- **Failure modes**: network errors, rate limits, auth errors. Reuse the tenacity pattern from `summarize._chat_openrouter` for retries. Keep it simple — fail loudly on auth errors, retry transient failures.
- **Cost transparency**: log estimated cost? Skip for now; the user can check Deepgram dashboard.

## Out of scope

- Streaming transcription (we're processing already-downloaded files).
- Switching diarization to a separate service when using whisperx (current pyannote setup stays).
- Migrating existing transcripts — keep them as-is. New runs use the configured backend.

## Phasing

1. Refactor `transcribe.py` into the dispatcher pattern (no behavior change).
2. Add Deepgram backend + config plumbing.
3. Wire CLI flags.
4. Manual test on one episode each backend.
5. Update `docs/plans/transcription-eval.md` with a comparison run if you want a quality/cost A/B.
