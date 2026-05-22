# Podracer

A local-first podcast and video transcription platform with speaker diarization, semantic search, and AI-powered insights.

## The Problem

I consume ~20 hours of podcasts per week, plus YouTube content. That's a massive amount of information that's locked in audio format:

- **Can't search it** - "What did that guest say about interest rates?" requires scrubbing through hours of audio
- **Can't reference it** - No way to quote or link to specific moments
- **Can't connect ideas** - Insights across episodes stay siloed in my memory
- **Cloud APIs are expensive** - At 80+ hours/month, transcription services cost $20-100+/month
- **Privacy concerns** - Sending all my listening habits to third parties

## The Solution

A pipeline you can mix-and-match between cloud and self-hosted:

- **Transcription** with speaker diarization — cloud (Deepgram) or self-hosted (WhisperX + pyannote)
- **Summarization** — cloud (OpenRouter) or local LLM (Ollama / vLLM)
- **Searchable archive** of everything you've ever listened to
- **Embeddings for semantic search** _(planned)_
- **AI chat / agents** over your archive _(planned)_

**Cost model**: heavy batch work (transcription, embedding, summarization) can run on a single homelab box for the cost of electricity. Cloud APIs are also first-class — pick per-stage. See [the cost comparison below](#cost-comparison-at-80-hoursmonth) for break-even math.

## What works today

- Subscribe to RSS feeds from the CLI or web UI
- Auto-download new episodes via a background worker
- Transcribe with speaker diarization (Deepgram in the cloud, or self-hosted WhisperX + pyannote)
- Summarize via OpenRouter, Ollama, or vLLM — chapters, insights, speaker takes
- Browse everything in a local web UI
- Inspect + manage the job queue from a `/jobs` dashboard
- Run as a long-running daemon via `systemctl --user`

### Performance

Rough numbers for a typical 2-hour podcast episode:

| Stage | Cloud (Deepgram + OpenRouter) | Local (WhisperX large-v3 on a single modern NVIDIA GPU) |
|-------|-------------------------------|--------------------------------------------------------|
| Transcribe + diarize | ~4 min, ~$0.50 | ~7 min, ~$0 (electricity) |
| Summarize | ~1.5 min, ~$0.10 | depends on local LLM throughput |

Cloud-only path needs **no GPU at all** and runs comfortably on a small VM or LXC (2 GB RAM is plenty).

### Quick start

See the [top-level README](../README.md) for install + first-run instructions.

## Roadmap

- **YouTube ingestion** alongside RSS
- **Embeddings + semantic search** across the transcript corpus
- **Entity extraction** (people, companies, concepts)
- **Audio player sync** in the web UI — click a chapter, jump to that moment
- **MCP server** so Claude or other agents can query your archive
- **Highlight + export** quotes and clips

## Why Local?

### Cost comparison (at 80 hours/month)

#### Transcription + Diarization only

| Service | Rate | Monthly (80 hrs) |
|---------|------|------------------|
| AWS Transcribe | $0.024/min | ~$115 |
| Google Speech-to-Text | $0.016/min + diarization | ~$95 |
| Deepgram | $0.0043/min + features | ~$25-40 |
| AssemblyAI | $0.15/hr + $0.02/hr diarization | **~$13.60** |
| **Local (electricity only)** | ~450W × 8min per 2hr file | **~$0.40** |

AssemblyAI is surprisingly cheap for transcription alone: **$13.60/month** for 80 hours.

#### But the full pipeline costs more

Podracer isn't just transcription - it's transcription + embeddings + summarization + search. Here's what the full cloud stack would cost:

| Service | What | Rate | Monthly (80 hrs) |
|---------|------|------|------------------|
| AssemblyAI | Transcription + diarization | $0.17/hr | $13.60 |
| OpenAI Embeddings | text-embedding-3-small | $0.02/1M tokens | ~$2-4 |
| Claude/GPT-4 | Summarization (~2K tokens out per episode) | $15/1M output tokens | ~$12-15 |
| Vector DB (cloud) | Pinecone/Weaviate hosted | $25-70/month | ~$25 |
| **Total cloud stack** | | | **~$55-110/month** |

#### What the numbers don't capture

- **Privacy** — your listening habits never leave your network
- **No vendor lock-in** — APIs change pricing, get deprecated, or add restrictions
- **Unlimited usage** — no per-request costs, re-process at will, experiment with larger models
- **Resale value** — hardware retains value; API spend is gone forever
- **Multi-use** — the same hardware can run other inference workloads (image generation, voice assistants, agentic tools)

That said, the "build a homelab" payoff is for people who already want hardware for other reasons. For pure economics on transcription alone, cloud transcription services are hard to beat — which is why podracer also supports Deepgram as a first-class backend.

## Architecture

Podracer separates orchestration from compute so each piece can scale or move independently:

```
                    SQLite (WAL)
                   ┌────────────────────────────┐
                   │ podcasts │ episodes │ jobs │
                   └─────▲──────▲──────────▲────┘
                         │      │          │
                ┌────────┴┐  ┌──┴──┐   ┌───┴───┐
                │   web   │  │ CLI │   │worker │
                │ FastAPI │  │     │   │       │
                └─────────┘  └─────┘   └───┬───┘
                                           │
                  ┌────────────────────────┼───────────────────────┐
                  ▼                        ▼                       ▼
            Transcription            Summarization               (future)
            • Deepgram (cloud)       • OpenRouter (cloud)        Embeddings
            • Whisperx-http          • Ollama / vLLM (local)     Vector search
              (self-hosted GPU)

```

- **SQLite (WAL)** is the source of truth: subscriptions, episodes, transcripts, summaries, and the job queue.
- **web** is a small FastAPI app that reads from the DB and renders HTML; lets you subscribe, browse, click "process this episode", and inspect the job queue.
- **worker** is a long-running process that syncs feeds, enqueues new episodes, and drains the queue. Idempotent stages — re-run safely.
- **CLI** does the same things as web + worker, for scripting and ad-hoc work.
- **Backends are HTTP-only.** The worker is a pure orchestrator; transcription and summarization run wherever you point them — cloud, local GPU server, or your laptop in a pinch.

This means a podracer install can range from "single CPU LXC using cloud APIs" all the way to "dedicated GPU server running whisperx + a local LLM" with no code changes — just configuration.

### Compute sizing

The web + worker host is intentionally small — it's a coordinator. The
expensive work happens behind HTTP backends.

| Component | Suggested specs | Notes |
|-----------|-----------------|-------|
| **Worker + web** | 2 CPU cores, 2 GB RAM, ~50 GB disk per ~250 episodes of audio | Idle workers use ~50 MB; spike to ~250 MB during cloud transcribe (audio read into memory). Disk grows with downloaded MP3s — most projects can safely drop audio post-summary if disk is a concern. |
| **Local LLM host** (optional) | Whatever VRAM fits your chosen model | Only needed if you don't want cloud LLMs. Anything that runs an Ollama or vLLM endpoint serves summarization. A single ~12 GB-class GPU runs a 14B/Q4 model comfortably. |
| **Whisperx host** (optional) | NVIDIA GPU with ≥8 GB VRAM | Only needed for self-hosted transcription. The `whisper-service` package preloads whisper-large-v3 + pyannote diarization. |

Cloud-only (Deepgram + OpenRouter) needs no GPU at all and runs on the
smallest VPS you can find.

