# Podracer — Project Roadmap

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

Still SQLite, still local files. Just a process. If you want it to survive reboots, wrap it in systemd or Docker — same as OpenClaw. Covers 99% of individual use cases.

The CLI still works alongside the daemon — commands can either execute directly (library calls) or hit the daemon's API, so the phone UI and terminal stay in sync.

### Tier 3: Platform (k8s deployment)

For enterprise/power-user scale — thousands of feeds, multiple users, heavy throughput. The daemon splits into separate processes:
- **Web server** — serves UI and API behind a load balancer
- **Workers** — GPU-aware transcription/summarization jobs with k8s scheduling
- **Scheduler** — feed sync, job dispatch

Postgres replaces SQLite. Object storage (MinIO/S3) replaces local files. GPU scheduling via taints/tolerations routes the right jobs to the right cards.

Same library, same code, just decomposed for scale.

### Why this matters for how we build

All three tiers use the same library. The design constraint is: **never bake deployment assumptions into the core.** Storage goes through a configurable path (local disk today, object store later). Database access goes through a thin abstraction (SQLite today, Postgres later). The CLI works identically whether you're running it on your laptop or inside a k8s pod.

## Iterative Development Strategy

Each component is built as a standalone CLI with its own eval harness. This means:
- Each piece is independently useful and testable
- Evals provide a regression safety net as models/params evolve
- The pipeline phase is just plumbing connecting proven components
- The UI phase consumes the same data the CLIs produce

## Phases

### Phase 1: Standalone CLIs

Can be worked in any order. Each has its own plan doc.

| Component | Plan Doc | Status |
|-----------|----------|--------|
| 1a. Podcast Search & Download | [podcast-search-download.md](podcast-search-download.md) | Not started |
| 1b. Transcription + Eval | [transcription-eval.md](transcription-eval.md) | CLI exists, needs eval |
| 1c. Summarization + Eval | [summarization-cli-eval.md](summarization-cli-eval.md) | Model evaluated, needs CLI + eval |

### Phase 2: Daemon + Job Queue

Orchestrates the full flow: download → transcribe → summarize → store.

- `podracer process <episode_id>` — enqueue full pipeline for an episode
- `podracer process --sync` — sync all subscriptions and enqueue new episodes
- `podracer status` — show running/pending/failed jobs
- `podracer cancel <job_id>` — cancel a pending or running job
- `podracer serve` — start the daemon (scheduler + web UI + API)

### Phase 3: Web/Mobile UI

- Browse transcripts with audio player sync
- Search across all transcripts (full-text + semantic)
- Highlight, annotate, export quotes
- Chat with archive (Claude API / MCP server)
- Job dashboard — see what's processing, queue depth, failures

## Shared Infrastructure

### Model Servers: Shared AI Infrastructure

Podracer talks to AI models through HTTP — always. Whether the model is local or cloud, the interface is the same: an HTTP request with a long timeout. This is enabled by **model servers** — lightweight HTTP wrappers around local models that manage VRAM and serve requests.

```
┌──────────────────────────────────────────────────────────────┐
│                      Model Servers                            │
│  (shared infrastructure — not podracer-specific)              │
│                                                               │
│  ┌──────────────────┐  ┌────────┐  ┌──────────────────────┐  │
│  │ Transcription     │  │ Ollama │  │ Embedding Server     │  │
│  │ Server (WhisperX) │  │ (LLM)  │  │ (nomic/BGE)         │  │
│  │ :8001             │  │ :11434 │  │ :8002                │  │
│  └──────────────────┘  └────────┘  └──────────────────────┘  │
│          ▲                  ▲               ▲                  │
│     OR cloud API       OR cloud API    OR cloud API           │
│     (AssemblyAI)       (Claude/GPT)    (OpenAI)               │
└──────────────────────────────────────────────────────────────┘
                    ▲        ▲        ▲
                    │        │        │
              ┌─────┴────────┴────────┴──────┐
              │      Podracer Daemon          │
              ├──────────────────────────────-┤
              │      OpenClaw                  │
              ├───────────────────────────────┤
              │      Voice Assistant           │
              ├───────────────────────────────┤
              │      Any future project        │
              └───────────────────────────────┘
```

The model servers are **reusable infrastructure**, not podracer internals. The transcription server is "WhisperX as an HTTP service" — any project on the network can call it. Ollama already works this way and is shared with OpenClaw and the voice assistant. This means:

- **One set of models in VRAM**, shared across all projects
- **No duplication** — OpenClaw and podracer both use Ollama for LLM inference
- **Provider swappable** — each server URL can point to a local server or a cloud API. The consuming app just sees an HTTP endpoint.

#### What we build vs. what already exists

| Server | Status | Notes |
|--------|--------|-------|
| Ollama (LLM inference) | Already running | Handles summarization, chat, embeddings (via `ollama/nomic-embed-text`) |
| Transcription server | **Needs to be built** | WhisperX wrapped in an HTTP server with idle VRAM unloading |
| Embedding server | Maybe not needed | Ollama supports embedding models. Separate server only if we need a dedicated model that Ollama doesn't serve. |

The transcription server is the main new piece. It follows the Ollama pattern:
- Persistent process, keeps model loaded with idle timeout
- Loads into VRAM on first request (~30s), unloads after N minutes of inactivity
- Single endpoint: `POST /transcribe` with audio file path, returns utterances
- Handles its own queuing internally (one transcription at a time)

#### Configuration

The daemon doesn't know what's behind the URL. It just calls the configured endpoint:

| Stage | Config Key | Default (local) | Cloud alternative |
|-------|-----------|-----------------|-------------------|
| Transcription | `transcription.url` | `http://localhost:8001` | `https://api.assemblyai.com/v2` |
| Summarization | `summarization.url` | `http://localhost:11434` | `https://api.anthropic.com/v1` |
| Embeddings | `embedding.url` | `http://localhost:11434` | `https://api.openai.com/v1` |
| Chat | `chat.url` | `http://localhost:11434` | `https://api.anthropic.com/v1` |

Each also has a `provider` key that tells the daemon which request/response format to use (since AssemblyAI and WhisperX don't speak the same protocol). The provider determines the adapter; the URL determines where it goes.

### Job Execution Model

**Core constraint**: transcription and summarization are long-running, GPU-bound, and cannot be parallelized on a single GPU. They must be queued and executed sequentially. A 2-hour podcast takes ~8 min to transcribe and ~2 min to summarize — you can't run two transcriptions at once on the same card.

**Design**: the daemon is a **scheduler that dispatches HTTP requests to model servers**, not a monolith that does everything in-process.

```
┌──────────────────────────────────────────────────────────────────┐
│                          Daemon                                   │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────────────────┐  │
│  │ Web UI / │  │Scheduler │  │  Worker                        │  │
│  │ API      │  │ (enqueue,│  │  (picks up jobs, dispatches    │  │
│  │          │  │  deps,   │  │   HTTP requests to model       │  │
│  │          │  │  retry)  │  │   servers or cloud APIs)       │  │
│  └──────────┘  └──────────┘  └────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
         │                              │
         │              ┌───────────────┼───────────────┐
         │              ▼               ▼               ▼
         │     localhost:8001    localhost:11434    api.anthropic.com
         │     Transcription     Ollama            Claude API
         │     Server            (summarization)   (chat, eval)
         │     (WhisperX)
         │
         ▼
    localhost:8000
    Web UI + API
```

#### Model servers with idle unloading

GPU-heavy work (transcription, local summarization) runs as **persistent HTTP servers** that manage their own VRAM lifecycle:

- **Transcription server** (`podracer transcription-server --port 8001`): loads WhisperX, accepts audio files, returns utterances
- **Ollama** (already works this way): serves LLM requests for summarization, embeddings, chat

These servers keep models loaded in VRAM with an **idle timeout** (like Ollama's default of 5 minutes):

1. Request comes in → load model into VRAM if not loaded (~30s for WhisperX)
2. Process the request
3. Start idle timer
4. No requests for N minutes → unload models, free VRAM
5. Next request → reload

This is strictly better than subprocess-per-job:
- **No repeated loading**: batch processing (10 new episodes) loads the model once
- **VRAM is freed when idle**: GPU isn't wasted when nothing is happening
- **Natural queuing**: server processes one request at a time; concurrent requests wait
- **Uniform interface**: the daemon doesn't care if it's hitting `localhost:8001` (local WhisperX) or `api.assemblyai.com` (cloud). Both are just HTTP with a long timeout.
- **Isolation**: if the transcription server crashes, the daemon stays up. Restart the server and jobs retry.

The daemon's worker just makes HTTP calls. The provider config determines the URL:

```
Tier 2: worker → POST localhost:8001/transcribe         → local GPU
Tier 2: worker → POST api.assemblyai.com/v2/transcript  → cloud API
Tier 3: worker → POST transcription-svc:8001/transcribe → k8s Service → GPU pod
```

#### Job table

```sql
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    job_type TEXT NOT NULL,           -- 'download' | 'transcribe' | 'summarize' | 'embed'
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending | blocked | running | completed | failed | cancelled
    parent_job_id INTEGER REFERENCES jobs(id),  -- must complete before this runs
    priority INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    run_after TEXT,                   -- backoff: don't pick up before this time
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    worker_id TEXT,                   -- which worker (for future multi-worker)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

#### Job chaining

`podracer process <episode_id>` enqueues a chain with dependencies:

```
download (pending) → transcribe (blocked) → summarize (blocked) → embed (blocked)
```

Each job unblocks the next when it completes. If a job fails and exhausts retries, its children stay blocked (don't propagate garbage).

#### Concurrency rules

- **Downloads**: can run in parallel (I/O bound, no GPU)
- **Transcription**: one at a time per server (server serializes requests internally)
- **Summarization via local LLM**: one at a time per Ollama instance
- **Summarization via API**: can run in parallel (no local resources, bounded by rate limits)
- **Embeddings**: can run in parallel with transcription (different model server)

For tier 2 (single machine), the model servers naturally serialize GPU work — no explicit locking needed. For tier 3 (k8s), the model servers are k8s Services with GPU pods behind them, and k8s handles scheduling.

### Database

SQLite — single file, no server, works for CLI and carries into the app phase.

**Tables:**
- `config` — key/value settings (e.g. `media_dir`, `ollama_url`)
- `podcasts` — subscribed shows (title, feed_url, artwork, etc.)
- `episodes` — individual episodes (title, published_at, audio_url, local_path, status)
- `transcripts` — transcript text + metadata (episode_id, model used, language, duration)
- `summaries` — structured summary JSON (episode_id, model used, schema version)
- `jobs` — background job queue (see schema above)

### File Storage

Audio and transcript files are stored on disk at a configurable `media_dir` path (default `./data/media/`). The path is stored in the `config` table.

**Future**: When deploying to k8s, `media_dir` will be swapped to an S3-compatible object store (e.g. MinIO). The abstraction boundary is the path — code reads/writes to `media_dir`, and the backing store is an infrastructure concern.

### CLI Structure

All commands go through a single `podracer` entrypoint.

**Design principle: lazy evaluation.** When you ask for a transcript or summary, podracer returns it immediately if it's already been processed. If not, it enqueues the work:

- **Daemon running** → enqueues jobs, returns immediately: "Episode queued for processing. Run `podracer status` to track progress."
- **Daemon not running** → runs the full pipeline synchronously (slow but works)

This means the CLI "just works" without requiring the daemon. The daemon is an optimization, not a requirement — important for the tier 1 FOSS experience.

**Design principle: human-friendly arguments.** Commands accept fuzzy names, not just IDs. Nobody wants to look up `episode_id 47832`.

```
# These should all work:
podracer summary "market huddle" "ep 284"
podracer summary "market huddle" "craig shapiro"
podracer transcript "market huddle" --latest

# Fuzzy matching against subscribed podcasts and their episodes.
# If ambiguous, prompt the user to clarify.
```

#### Full command reference

```
# Discovery
podracer search <query>                   # Search Podcast Index
podracer episodes <podcast> [--limit N]   # List episodes (by name or ID)

# Subscription management
podracer subscribe <podcast>              # Subscribe to a show
podracer unsubscribe <podcast>            # Remove subscription
podracer sync                             # Trigger immediate sync (daemon: pokes the scheduler;
                                          # no daemon: runs synchronously)

# Content retrieval (lazy — returns cached or enqueues processing)
podracer transcript <podcast> <episode>   # Get transcript
podracer summary <podcast> <episode>      # Get summary
podracer download <podcast> <episode>     # Download audio only

# Pipeline control
podracer process <podcast> [<episode>]    # Enqueue full pipeline
podracer status                           # Show job queue
podracer cancel <job_id>                  # Cancel a job

# Daemon
podracer serve                            # Start daemon (scheduler + web UI + API)
                                          # Auto-syncs subscriptions on a configurable
                                          # interval (default: every 30 min). New episodes
                                          # are automatically downloaded and processed.

# Ask questions about episodes
podracer ask "market huddle" "ep 284" "What did Craig say about gold?"
podracer ask "market huddle" --latest "Summarize the investment thesis"
                                          # Sends transcript + summary + system prompt
                                          # to configured chat model (Claude by default).
                                          # Uses the same provider abstraction as summarization.

# Low-level (file-based, for scripting and evals)
podracer transcribe <file>                # Transcribe an audio file directly
podracer summarize <file>                 # Summarize a transcript file directly

# Evals
podracer eval-transcribe                  # Run transcription eval
podracer eval-summarize                   # Run summarization eval
```

### Configuration

Managed via SQLite `config` table + environment variables. Env vars override DB values.

**Design principle: every model is swappable.** Podracer should never be married to a specific model for any task. Users pick the models that fit their hardware, quality requirements, and budget. The evals exist precisely so you can swap a model and measure the impact.

#### Model configuration

Each pipeline stage has its own model config. Defaults are opinionated but everything is overridable.

| Stage | Config Key | Default | What it controls |
|-------|-----------|---------|-----------------|
| Transcription | `transcription.model` | `large-v3` | Whisper model size (tiny/base/small/medium/large-v3) |
| Transcription | `transcription.device` | `cuda` | cuda / cpu |
| Transcription | `transcription.compute_type` | `float16` | float16 / int8 / float32 |
| Diarization | `diarization.enabled` | `true` | Enable/disable speaker diarization |
| Summarization | `summarization.provider` | `ollama` | ollama / openai / anthropic / custom |
| Summarization | `summarization.model` | `gemma4:e4b` | Model name (provider-specific) |
| Summarization | `summarization.context_window` | `65536` | num_ctx for Ollama, max_tokens for APIs |
| Embeddings | `embeddings.provider` | `ollama` | ollama / openai / custom |
| Embeddings | `embeddings.model` | `nomic-embed-text` | Model name |
| RAG/Chat | `chat.provider` | `anthropic` | ollama / openai / anthropic |
| RAG/Chat | `chat.model` | `claude-sonnet-4-20250514` | Model name |
| Daemon | `sync.interval_minutes` | `30` | How often to check feeds for new episodes |
| Daemon | `sync.auto_process` | `true` | Automatically process (transcribe + summarize) new episodes |

This means you could run podracer with:
- **All local**: Whisper + gemma4 + nomic-embed — zero API costs
- **All cloud**: AssemblyAI + Claude + OpenAI embeddings — no GPU needed
- **Hybrid**: local transcription + Claude for summarization — best of both
- **Budget**: Whisper tiny + Qwen 8B + BGE-small — runs on a laptop GPU

#### General configuration

| Setting | Default | Env Override |
|---------|---------|-------------|
| `media_dir` | `./data/media/` | `PODRACER_MEDIA_DIR` |
| `db_path` | `./data/podracer.db` | `PODRACER_DB` |
| `ollama_url` | `http://localhost:11434` | `OLLAMA_URL` |
| `hf_token` | (from .credentials) | `HF_TOKEN` |
| `podcast_index_key` | — | `PODCAST_INDEX_KEY` |
| `podcast_index_secret` | — | `PODCAST_INDEX_SECRET` |
| `anthropic_api_key` | — | `ANTHROPIC_API_KEY` |
| `openai_api_key` | — | `OPENAI_API_KEY` |

### Project Layout

```
podracer/
  podracer/
    __init__.py
    cli.py              # Main CLI entrypoint (argparse/click)
    db.py               # SQLite connection + migrations
    config.py           # Config resolution (DB + env vars)
    search.py           # Podcast Index API client
    feed.py             # RSS feed parsing
    download.py         # Episode download
    transcribe.py       # Transcription (exists)
    summarize.py        # Summarization via Ollama
    eval/
      transcription.py  # WER/DER eval harness
      summarization.py  # LLM-as-judge eval harness
  data/
    media/              # Downloaded audio files
    podracer.db         # SQLite database
  eval/
    transcription/      # Eval dataset (clips + references)
    summarization/      # Eval dataset (transcripts + golden summaries)
  docs/
    plans/              # Planning documents
  pyproject.toml
  Dockerfile
```

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Podcast API | Podcast Index API | Open-source, richer metadata, free with API key |
| Summarization eval judge | Claude API | Higher quality judgments than self-judging with the same local model |
| Audio storage | Configurable path, default `./data/media/` | Simple for dev, swap to object store for deployment |
| Database | SQLite | No server dependency, single file, sufficient for CLI + small-scale app |
| Summarization model | gemma4:e4b via Ollama | 128k context, fits in VRAM alongside other models, already evaluated |
| Transcription model | WhisperX (large-v3) | Already working in MVP |
