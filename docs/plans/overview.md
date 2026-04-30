# Podracer — Project Roadmap

**Last updated:** 2026-04-25

## Vision

A local-first podcast and video knowledge platform: ingest audio, transcribe, summarize, search, and chat with your archive. Heavy compute runs on local GPUs; cloud APIs reserved for interactive queries.

## Architecture: Library-First, Deploy Anywhere

The core of podracer is a **library** — pure functions that search, download, transcribe, and summarize. Everything else is a thin shell over it. This enables three deployment tiers from the same codebase:

```
                    ┌─────────────┐
                    │   Library   │  ← the actual product
                    └──────┬──────┘
          ┌────────────┬───┴───┬────────────┐
          │            │       │            │
        CLI       Daemon    k8s Workers  MCP/Agent
```

### Tier 1: CLI (`pip install podracer`)

Run commands manually. SQLite database, files on disk, local GPU (or CPU). No server, no accounts, no infrastructure. The bar to entry is `uv tool install podracer`.

**This is the FOSS story.** Anyone with a machine can install it and have a working podcast knowledge base. Simple enough to be popular.

### Tier 2: Daemon (`podracer serve`)

A single long-running process on any machine — your laptop, a GPU tower, a VPS. Does three things:
1. **Serves the web UI** — browse summaries, search your archive, read on your phone
2. **Runs the scheduler** — syncs feeds, transcribes, summarizes automatically
3. **Exposes the API** — same endpoints the CLI and agents use

Still SQLite, still local files. Just a process. Covers 99% of individual use cases.

### Tier 3: Platform (k8s deployment)

For enterprise/power-user scale. Postgres replaces SQLite. Object storage replaces local files. GPU scheduling via k8s routes jobs to the right cards.

Same library, same code, just decomposed for scale.

## Current Status

### What's built and working

| Component | Status | Notes |
|-----------|--------|-------|
| **Transcription CLI** | Done | WhisperX + pyannote diarization. ~8 min for 2-hour podcast on RTX 5090. |
| **Summarization CLI** | Done | Multi-pass pipeline (speakers → summary → chapters → insights → takes). Three backends: Ollama, vLLM, OpenRouter. |
| **Model evaluation** | Research done | Tested 6 model/backend combos. Best: Qwen 3.6 35B MoE (quality) or Gemma 4 E4B bf16 (speed). See [eval results](2026-04-24-summarization-eval-results.md). |
| **Backend abstraction** | Done | `Backend` dataclass with Ollama/vLLM/OpenRouter support. Unified thinking-model handling. |

### What's planned but not started

| Component | Plan doc | Priority |
|-----------|----------|----------|
| **Podcast search & download** | [podcast-search-download.md](podcast-search-download.md) | Next — needed to build the pipeline |
| **Summarization eval** | [summarization-cli-eval.md](summarization-cli-eval.md) | High — need multi-transcript eval to confirm model rankings |
| **Transcription eval** | [transcription-eval.md](transcription-eval.md) | Medium — transcription works, eval is for regression testing |
| **Pipeline orchestration** | This doc (Phase 2) | After Phase 1 components are solid |

## Phases

### Phase 1: Standalone CLIs (current)

Each component is independently useful and testable.

| Step | Component | Plan Doc | Status |
|------|-----------|----------|--------|
| 1a | Podcast search & download | [podcast-search-download.md](podcast-search-download.md) | Not started |
| 1b | Transcription polish + eval | [transcription-eval.md](transcription-eval.md) | CLI done, eval not started |
| 1c | Summarization eval | [summarization-cli-eval.md](summarization-cli-eval.md) | CLI done, eval not started |

**Suggested order:** 1a → 1c → 1b

1a is the bottleneck — without podcast discovery and download, there's no pipeline. 1c (summarization eval) is high value because we need multi-transcript testing to confirm model choices. 1b (transcription eval) is lower priority since transcription already works well.

#### Phase 1a: Podcast Search & Download

Build the podcast registry: search Podcast Index API, browse episodes, subscribe to feeds, download audio. SQLite-backed with CLI commands.

Key deliverables:
- `podracer search <query>` — search Podcast Index
- `podracer episodes <podcast>` — list episodes from RSS
- `podracer subscribe <podcast>` / `podracer sync` — subscription management
- `podracer download <podcast> <episode>` — download audio
- SQLite schema for podcasts, episodes, config

Dependencies: `feedparser`, `httpx` (already have), `rich` (nice-to-have for progress bars)

Full spec: [podcast-search-download.md](podcast-search-download.md)

#### Phase 1b: Transcription Polish + Eval

The transcription CLI works but could use:
- Eval harness (WER/DER metrics via jiwer + pyannote.metrics)
- 3-5 hand-verified reference transcripts for regression testing
- `--json` output for machine consumption

Not urgent — transcription quality is already good. Eval matters when we want to test different Whisper model sizes or swap to a different transcription engine.

Full spec: [transcription-eval.md](transcription-eval.md)

#### Phase 1c: Summarization Eval

The summarization CLI works across three backends with structured output. What's missing:
- Multi-transcript evaluation (current testing is on one episode only)
- Claude-as-judge scoring on accuracy, completeness, attribution, conciseness
- Automated comparison across models/backends
- `podracer eval-summarize --dataset <path>`

This is high priority — we need to confirm that E4B bf16 quality holds across diverse episodes before committing to it as the production model.

Full spec: [summarization-cli-eval.md](summarization-cli-eval.md)

### Phase 2: Pipeline CLI + Job Queue

Wire the standalone CLIs into an end-to-end pipeline.

```
subscribe → sync → download → transcribe → summarize → store
```

Key deliverables:
- `podracer process <podcast> [<episode>]` — run full pipeline
- Job queue in SQLite with dependency chaining (download → transcribe → summarize)
- Lazy evaluation: return cached results if already processed, enqueue if not
- `podracer status` — show job queue

Design principle: **the daemon is optional.** Without a daemon, `podracer process` runs synchronously. With a daemon, it enqueues and returns immediately.

### Phase 3: Daemon + Web UI

- `podracer serve` — scheduler + web UI + API
- Auto-sync subscriptions on interval
- Browse transcripts with audio player sync
- Search across all transcripts (full-text + semantic)
- Chat with archive (Claude API / MCP server)
- Job dashboard

## Inference Architecture

### Model Servers: Shared AI Infrastructure

Podracer talks to AI models through HTTP — always. Whether the model is local or cloud, the interface is the same.

```
┌──────────────────────────────────────────────────────────┐
│                    Model Servers                          │
│  ┌──────────────┐  ┌─────────────┐  ┌────────────────┐  │
│  │ Transcription │  │ LLM Server  │  │ Embedding      │  │
│  │ (WhisperX)    │  │ (Ollama/    │  │ Server         │  │
│  │               │  │  vLLM)      │  │                │  │
│  └──────────────┘  └─────────────┘  └────────────────┘  │
│     OR cloud           OR cloud          OR cloud        │
│     (AssemblyAI)       (OpenRouter)      (OpenAI)        │
└──────────────────────────────────────────────────────────┘
```

### Current Backend Support (Summarization)

| Backend | Structured Output | Batching | Thinking Control | Auth |
|---------|------------------|----------|-----------------|------|
| Ollama | `format` schema | No | `think: false` | None |
| vLLM | `response_format.json_schema` | Yes (continuous batching) | `chat_template_kwargs` | None |
| OpenRouter | `response_format.json_schema` | N/A (cloud) | `reasoning.effort: none` | API key |

### VRAM Budget (RTX 5090, 32GB)

| Model | Best backend | VRAM | Speed | Quality |
|-------|-------------|------|-------|---------|
| Gemma 4 E4B | vLLM bf16 | ~22 GB | ~87s/ep | Good |
| Qwen 3.6 35B MoE | Ollama Q4 | ~23 GB | ~210s/ep | Excellent |
| Qwen 3.6 35B MoE | OpenRouter | N/A | ~45s/ep | Excellent |

27B+ dense models do not fit on single 32GB GPU via vLLM. See [inference backends](2026-04-23-inference-backends.md) and [eval results](2026-04-24-summarization-eval-results.md) for details.

## Database

SQLite — single file, no server, works for CLI and carries into the app phase.

**Tables:** config, podcasts, episodes, transcripts, summaries, jobs

Full schema in [podcast-search-download.md](podcast-search-download.md) (podcasts/episodes) and this doc (jobs).

## CLI Structure

All commands go through a single `podracer` entrypoint with `--json` output for agent consumption.

```
# Discovery & subscription
podracer search <query>
podracer episodes <podcast> [--limit N]
podracer subscribe <podcast>
podracer sync

# Content (lazy — returns cached or enqueues processing)
podracer download <podcast> <episode>
podracer transcript <podcast> <episode>
podracer summary <podcast> <episode>

# Pipeline
podracer process <podcast> [<episode>]
podracer status

# Low-level (file-based, for scripting and evals)
podracer transcribe <file>
podracer summarize <file> --backend <backend> --model <model>

# Evals
podracer eval-transcribe --dataset <path>
podracer eval-summarize --dataset <path>

# Daemon
podracer serve
```

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Podcast API | Podcast Index API | Open-source, free with API key |
| Summarization models | Qwen 3.6 35B MoE (quality), Gemma 4 E4B (speed) | Evaluated across 6 combos, see eval results |
| Inference backends | Ollama + vLLM + OpenRouter | Local GGUF, local bf16, and cloud — covers all deployment scenarios |
| Summarization eval judge | Claude API | Higher quality judgments than self-judging with the local model |
| Audio storage | Configurable `media_dir`, default `./data/media/` | Simple for dev, swap to object store for deployment |
| Database | SQLite | No server dependency, sufficient for CLI + daemon |
| Transcription | WhisperX (large-v3) | Already working, ~8 min for 2-hour podcast |
