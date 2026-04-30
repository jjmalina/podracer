# Phase 1b: Transcription Eval

## Goal

Build an eval harness for the existing transcription CLI so we can confidently change models, parameters, or WhisperX versions without regressions.

## Current State

The transcription CLI (`podracer transcribe`) works — it produces speaker-diarized transcripts via WhisperX + pyannote. What's missing is a way to measure quality.

## Metrics

### Word Error Rate (WER)

Standard speech recognition metric. Measures insertions, deletions, and substitutions against a reference transcript.

- Library: `jiwer` (pip install jiwer)
- Normalization: lowercase, strip punctuation, collapse whitespace before comparison
- Report per-clip and aggregate (macro average)

### Diarization Error Rate (DER)

Measures how well speaker labels match ground truth, accounting for:
- Missed speech (speaker talking but not detected)
- False alarm (silence labeled as speech)
- Speaker confusion (wrong speaker label)

- Library: `pyannote.metrics` (already have pyannote as a dependency)
- Requires time-aligned speaker labels in both hypothesis and reference

### Speaker Count Accuracy

Simple: did we detect the right number of speakers? Report as exact match rate.

## Eval Dataset

### Format

Each eval sample is a directory:

```
eval/transcription/
  sample-01/
    audio.mp3          # Short clip (2-5 minutes)
    reference.txt      # Ground truth transcript (speaker-labeled)
    metadata.json      # { "speakers": 2, "duration_seconds": 180, "source": "..." }
  sample-02/
    ...
```

Reference transcript format (matches our output format):
```
[SPEAKER_00] Welcome back to the show. Today we have a special guest.
[SPEAKER_01] Thanks for having me. Excited to be here.
```

### Creating the Dataset

- Start with 3-5 clips from podcasts we've already transcribed
- Cut short segments (2-5 min) with clear speaker turns
- Hand-verify the transcript against the audio — correct errors, fix speaker labels
- This is manual work but only needs to be done once; the dataset is reusable

### Guidelines

- Include variety: 2-speaker interviews, 3-speaker panels, single-speaker monologues
- Include at least one clip with overlapping speech (hard case for diarization)
- Include at least one clip with background noise or music

## CLI Interface

```
$ podracer eval-transcribe --dataset eval/transcription/

Transcription Eval
══════════════════

  Sample          WER     DER     Speakers (pred/ref)
  ──────────────  ──────  ──────  ───────────────────
  sample-01       4.2%    8.1%    2 / 2 ✓
  sample-02       6.8%    12.3%   3 / 3 ✓
  sample-03       3.1%    15.7%   2 / 3 ✗
  ──────────────  ──────  ──────  ───────────────────
  Aggregate       4.7%    12.0%   66.7%

Run with --json for machine-readable output.
```

With `--json`:
```json
{
  "samples": [
    {
      "name": "sample-01",
      "wer": 0.042,
      "der": 0.081,
      "speakers_predicted": 2,
      "speakers_reference": 2
    }
  ],
  "aggregate": {
    "wer_mean": 0.047,
    "der_mean": 0.120,
    "speaker_accuracy": 0.667
  }
}
```

The `--json` flag is important for agent consumption — an AI agent can run the eval and parse the results programmatically.

## Implementation

```
podracer/
  eval/
    __init__.py
    transcription.py   # Eval harness: load dataset, run transcription, compute metrics
```

Key functions:
- `load_eval_dataset(path)` — reads sample dirs, returns list of (audio_path, reference_text, metadata)
- `compute_wer(hypothesis, reference)` — normalize + jiwer
- `compute_der(hypothesis_segments, reference_segments)` — pyannote.metrics
- `run_eval(dataset_path, model, device, compute_type)` — orchestrate, return results dict

## Transcription Provider Abstraction

Unlike summarization (where Pydantic AI can unify LLM calls), there's no off-the-shelf library that abstracts across speech-to-text providers. The providers have fundamentally different interfaces and output formats:

| Provider | Interface | Diarization | Word timestamps | Notes |
|----------|-----------|-------------|-----------------|-------|
| WhisperX (local) | Python library, loads into GPU | Via pyannote | Yes | Current implementation |
| faster-whisper | Python library, local | No (separate) | Yes | Lighter alternative to WhisperX |
| OpenAI Whisper API | REST, sync | No | Yes | Simple but no diarization |
| AssemblyAI | REST, async (poll) | Yes (built-in) | Yes | Best cloud option for features |
| Deepgram | REST, sync or streaming | Yes (built-in) | Yes | Fast, good for real-time |
| Google Speech-to-Text | REST, streaming or batch | Yes | Yes | Complex auth |

### Common output format

All providers normalize into a shared structure:

```python
@dataclass
class Word:
    text: str
    start: float
    end: float
    speaker: str | None

@dataclass
class Utterance:
    speaker: str
    text: str
    start: float
    end: float
    words: list[Word]  # optional word-level timestamps
```

Each provider gets a thin adapter (~50 lines) that calls its API and returns `list[Utterance]`. The rest of the pipeline only sees utterances.

### CPU fallback

WhisperX supports CPU mode (`--device cpu --compute-type int8`). It's ~10-15x slower — a 2-hour podcast takes 1-2 hours instead of 8 minutes — but it works. This keeps the FOSS tier 1 story viable: anyone can install podracer and transcribe without a GPU. For faster results without hardware, configure a cloud provider.

### Distributed transcription challenge

WhisperX loads ~6GB of models into GPU VRAM. This creates a tension for the daemon deployment:

- If the daemon runs on a machine without a GPU, it can't transcribe locally
- If it runs on the GPU machine, the web UI is tied to that box
- Dispatching transcription to a remote GPU (SSH, job queue, microservice) is basically reinventing k8s scheduling

For tier 2 (single daemon), the simplest answer is: **run the daemon on the GPU machine**, access the UI over the network. For tier 1 (CLI), you either have a local GPU, use CPU mode, or configure a cloud provider.

The tier 3 (k8s) model solves this cleanly: web UI runs anywhere, transcription jobs get scheduled to GPU nodes via taints/tolerations.

### Phase plan

**Phase 1**: WhisperX directly (current). Define the `Utterance` output format as a Pydantic model.
**Phase 2**: Add provider adapters behind a common interface. Provider is a config value (`transcription.provider` + `transcription.model`).

## Dependencies

| Package | Purpose |
|---------|---------|
| `jiwer` | WER computation |
| `pyannote.metrics` | DER computation (may already be installed with pyannote.audio) |

## Acceptance Criteria

- [ ] Eval dataset with 3+ hand-verified samples exists
- [ ] `podracer eval-transcribe` runs all samples and prints scorecard
- [ ] `--json` flag outputs machine-readable results
- [ ] WER and DER metrics are computed correctly (verify against manual calculation on one sample)
- [ ] Eval runs end-to-end on GPU (with --device cpu fallback)
