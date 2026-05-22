# Systemd Daemon: Web + Worker

**Date:** 2026-05-12
**Status:** Planned
**Implements:** Phase 3 (Daemon) from [overview.md](overview.md)
**Related:** [Deepgram backend](2026-05-18-deepgram-backend.md), [Whisper service (deferred)](2026-05-12-whisper-service.md)

## V1 deployment posture (2026-05-18 update)

This plan was originally written assuming whisperx-in-process on a GPU host. We're now landing in two stages:

- **v1 (this plan + Deepgram backend):** Deploy worker + web on a CPU-only LXC. Transcription via Deepgram, summarization via OpenRouter. No GPU on the runtime host. Worker becomes a pure HTTP orchestrator. Most of the GPU/VRAM discussion below is **forward-looking** for when the GPU host comes online — it does not gate v1.
- **v2 (whisper-service plan):** When the RTX 4090 server is racked, add `podracer-whisper.service` on the GPU host and switch `transcribe_backend` from `deepgram` to `whisperx-http`. The worker LXC is unchanged.

Net effect on this plan: the dispatcher in `transcribe_episode` picks a backend per `cfg.transcribe_backend`. Everything else (jobs table, watermark, drain loop, systemd units) is identical in both deployment modes.

## Goal

Run podracer unattended on a single machine via two `systemctl --user` services:

1. **`podracer-web.service`** — long-running FastAPI web UI (already works via `podracer serve`).
2. **`podracer-worker.service`** — long-running scheduler + job worker. Periodically syncs subscribed feeds, enqueues newly published episodes, and drains a SQLite-backed job queue through the existing download → transcribe → summarize pipeline.

A single shared SQLite database is the source of truth, the queue, and the watermark store. No external broker.

## Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Process model | Split web + worker | Failure isolation. Web stays up while worker runs hours-long jobs. Worker can be restarted without dropping web traffic. |
| Episode selection | Only new from now on (watermark) | Subscribing doesn't trigger a backlog dump. Watermark = max `episodes.created_at` seen by the worker. Old pending episodes stay manual. |
| Systemd scope | `systemctl --user` | No root. Direct access to `.venv`, GPU, HOME. `loginctl enable-linger` lets it start at boot without login. |
| Queue backend | SQLite `jobs` table | Same DB as everything else. WAL mode already enabled. Single worker drains it — no broker needed. |
| Worker model | Single-threaded serial drain (v1) | Transcription holds the GPU in-process, so even with non-GPU summarize jobs we serialize for now. Concurrency comes for free when transcription gets extracted to a model server. |
| Scheduler trigger | In-process timer | Worker wakes every `sync_interval_minutes`. No separate `.timer` unit. Simpler unit set; one process to reason about. |
| Job granularity | Two kinds: `transcribe` + `summarize` (with dep) | Failure granularity (re-summarize without re-transcribing), clearer status visibility, and future-ready when transcription extracts to its own service. Download stays bundled into `transcribe` — `transcribe_episode` fetches if `local_path` is missing. |
| Pipeline reuse | Factor stages into `podracer/process.py::{transcribe_episode, summarize_episode, process_episode}` | Worker calls each stage independently per job kind. CLI's `podracer process` calls the convenience wrapper. |
| Transcription compute | Backend-dispatched in `transcribe_episode` | v1 uses Deepgram (HTTP, no local compute). Future GPU host serves whisperx via HTTP. The original whisperx-in-process path stays available for local dev. The worker is a pure HTTP orchestrator in v1; GPU coupling discussion below applies only if we ever run the worker on a GPU host with in-process whisperx. |

## Architecture

```
                   ┌───────────────────────────────────────────┐
                   │              SQLite (WAL)                  │
                   │   podcasts │ episodes │ jobs │ config      │
                   └────────▲─────────▲─────────────▲──────────┘
                            │         │             │
                ┌───────────┘         │             └────────────┐
                │                     │                          │
       ┌────────┴────────┐     ┌──────┴──────┐          ┌───────┴────────┐
       │  podracer-web   │     │ podracer-   │          │  podracer CLI   │
       │  .service       │     │ worker      │          │  (interactive)  │
       │                 │     │ .service    │          │                 │
       │  FastAPI :8080  │     │             │          │  manual         │
       │  read-only-ish  │     │  scheduler  │          │  invocations    │
       └─────────────────┘     │  + queue    │          └─────────────────┘
                               │  drain      │
                               └─────────────┘
```

### Worker loop (single iteration)

```
1. sync_subscribed_feeds(conn)
     - for each podcast WHERE subscribed=1: fetch RSS, upsert_episode(...)
     - update_podcast_synced(...)
2. enqueue_new_episodes(conn)
     - for each new episode (created_at > watermark, podcast subscribed,
                              no active job already):
         t = INSERT job (kind='transcribe',  status='queued')
         _ = INSERT job (kind='summarize',   status='queued',
                         depends_on_job_id=t.id)
3. advance_watermark(conn, now)
4. drain_queue(conn, cfg)
     - while a job is claimable (queued AND dep is done-or-null):
         job = claim_next_job(conn)   -- atomic UPDATE…RETURNING
         if shutdown_event.is_set(): break
         try:
             dispatch(job):
                 'transcribe' -> transcribe_episode(conn, cfg, job.episode_id)
                 'summarize'  -> summarize_episode(conn, cfg, job.episode_id)
             mark_job_done(conn, job.id)
         except Exception:
             mark_job_failed(conn, job.id, error)
             if attempts >= max_attempts:
                 cascade_block_dependents(conn, job.id)
5. sleep_until(next_sync_time, shutdown_event)
```

A single worker thread serially claims and runs jobs of any kind in v1. Because `transcribe` jobs hold the GPU in-process, parallelism would only help `summarize` jobs (cloud HTTP); we defer that until transcription is extracted to a service. See [GPU / In-Process Compute](#gpu--in-process-compute).

## GPU / In-Process Compute

> **Note (2026-05-18):** This section is forward-looking. In the v1 deployment (Deepgram backend, no local GPU) the worker is pure HTTP — none of the GPU concerns below apply. Keep this section as the design contract for when we run whisperx in-process again (e.g., a dev box with a GPU, or before the whisper-service is extracted on the GPU host).

**Transcription, when run locally, is in-process GPU work.** That asymmetry shapes several design choices below.

| Stage | Compute location | What the worker does |
|-------|-----------------|----------------------|
| Download | Local disk + HTTP | Saves audio file. |
| Transcribe | **Worker process VRAM** (whisperx + pyannote diarization) | Loads model into GPU, runs Whisper, runs diarization, writes transcript. |
| Summarize (Ollama / vLLM) | Local model server (separate process) | HTTP call. |
| Summarize (OpenRouter) | Cloud | HTTP call. |

### Implications

- **Serial drain in v1.** Even though `summarize` jobs don't use the GPU, the worker drains the queue serially. Reason: transcription happens inside the worker process, and we want one predictable code path before we introduce concurrency. We accept that a backlog of summarize jobs blocks behind transcribe jobs for now.
- **Worker IS the GPU process.** Restarting `podracer-worker.service` reloads whisperx + diarization weights from disk (~10–30s). Acceptable, but inform `RestartSec` and `TimeoutStopSec` accordingly.
- **Web service has no torch import.** `podracer/web/` doesn't touch `transcribe`/`torch` at all. If we ever deploy web and worker on different machines, this split is already clean — they share only the DB.
- **GPU contention with local LLM backends.** If `summarize_backend = "vllm"` or `"ollama"` runs on the same GPU, transcription and a local summarizer compete for VRAM. Two safe configurations on a 32 GB GPU:
  - **Recommended for the daemon:** `summarize_backend = "openrouter"`. Worker's whisperx is the only local VRAM consumer.
  - Worker transcribes, releases VRAM, then summarizes via local vLLM. Sequential per episode in our serial loop. Add explicit `del model; gc.collect(); torch.cuda.empty_cache()` at the end of `transcribe()` to surface VRAM back to the local LLM backend before the summarize HTTP call.
- **OOM crashes the worker.** GPU OOM during transcription kills the worker process. systemd restart → orphan recovery → job retried. After `max_attempts` it's marked failed and surfaced via `podracer status`.
- **Worker imports torch eagerly *only when configured for the in-process backend*.** Under Deepgram (or any HTTP transcribe backend), the worker never imports torch — keeps the LXC slim. Under `transcribe_backend = "whisperx"`, the worker imports `podracer.transcribe` at process start so CUDA/torch problems surface immediately rather than on first job.
- **Multi-GPU.** Out of scope for v1 — set `Environment=CUDA_VISIBLE_DEVICES=0` if needed. A future templated unit `podracer-worker@N.service` pinned per GPU is the natural extension, but it requires job partitioning we don't need yet.

### Future: extract transcription to a model server

Long term, transcription should live behind an HTTP API like summarization does (e.g., a `whisper-server` we run alongside Ollama/vLLM). When that happens:

- The worker no longer imports torch. It becomes a pure orchestrator making HTTP calls for every stage.
- Worker restart cost drops to near-zero (no model reload).
- The drain loop can run jobs concurrently — multiple `summarize` jobs in flight, multiple `transcribe` jobs in flight against a batching whisper server.
- Web and worker become symmetric and can be deployed on machines without GPUs.

The two-kind job model in this plan (`transcribe` + `summarize`) is chosen partly so this extraction is a code change inside the handlers, not a queue redesign.

## Data Model

### New table: `jobs`

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    kind TEXT NOT NULL,                     -- 'transcribe' | 'summarize'
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed | blocked
    depends_on_job_id INTEGER REFERENCES jobs(id),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_depends_on
    ON jobs(depends_on_job_id);

-- One active (queued/running) job per (episode, kind). Multiple done/failed
-- rows are allowed (re-runs via --force or after failure cleanup).
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_unique
    ON jobs(episode_id, kind) WHERE status IN ('queued', 'running');
```

`status` semantics:

| Status | Meaning |
|--------|---------|
| `queued` | Ready to run when dependency (if any) is `done`. |
| `running` | A worker has claimed it. |
| `done` | Finished successfully. |
| `failed` | Exhausted `max_attempts`. Surfaced via `podracer status`. |
| `blocked` | Upstream dep ended in `failed`; we won't run this one. User can `--force` to retry the chain. |

### Watermark storage

Reuse the existing `config` table:

```
INSERT INTO config (key, value) VALUES ('worker_watermark', '<ISO8601 timestamp>');
```

On worker startup with no prior watermark: initialize to `datetime('now')` so backlog is NOT auto-processed.

### Orphan recovery

On worker startup, reset any `status='running'` jobs from the previous run:

```sql
UPDATE jobs SET status='queued', last_error='worker restarted mid-job'
WHERE status='running';
```

The pipeline is idempotent (already skips existing transcripts/summaries), so re-running a partially-completed job is safe.

## Config Additions

`config.toml`:
```toml
[daemon]
sync_interval_minutes = 30      # how often the worker syncs feeds + enqueues
max_attempts = 3                # per-job retry budget
retry_backoff_seconds = 300     # base for exponential backoff between retries
```

Add to `podracer/config.py::Config`:
```python
sync_interval_minutes: int = 30
max_attempts: int = 3
retry_backoff_seconds: int = 300
```

Loaded from `[daemon]` section, with env var overrides:
- `PODRACER_SYNC_INTERVAL_MINUTES`
- `PODRACER_MAX_ATTEMPTS`

## CLI Changes

### New: `podracer worker`

Long-running worker. This is what `podracer-worker.service` invokes.

```
$ podracer worker [--once]

  --once   Run a single iteration and exit (for testing / manual cron-style use)
```

Behavior:
- Reads config, opens DB, runs orphan recovery.
- Installs signal handlers for `SIGTERM` and `SIGINT` that set a `shutdown_event`.
- Enters the worker loop. Between stages, checks `shutdown_event` to allow graceful exit.
- On `--once`: runs exactly one iteration (sync + drain) then exits.

### New: `podracer status`

Show queue state for ops/debugging.

```
$ podracer status

Worker:
  Last sync:      2026-05-12 14:32:11 (3 min ago)
  Watermark:      2026-05-12 14:32:11
  Sync interval:  30 min

Jobs:
  queued:     2
  running:    1  (episode 41: transcribing, started 2 min ago)
  done:      87
  failed:     1  (episode 12, last error: "OOM during transcription")

Subscribed podcasts: 5
```

`--json` outputs the same data as JSON.

### Modified: `podracer serve`

No behavior change. Keep `--reload` for development. This is what `podracer-web.service` invokes (without `--reload`).

### Refactor: pipeline stages → `podracer/process.py`

Split the pipeline body into per-stage functions plus a convenience wrapper:

```python
# podracer/process.py
def transcribe_episode(
    conn: sqlite3.Connection,
    cfg: Config,
    episode_id: int,
    *,
    force: bool = False,
) -> None:
    """Ensure the episode is downloaded, then transcribe it.
    Skips if a transcript already exists (unless force). Idempotent.
    Holds GPU VRAM during execution."""


def summarize_episode(
    conn: sqlite3.Connection,
    cfg: Config,
    episode_id: int,
    *,
    force: bool = False,
    backend: str | None = None,
    model: str | None = None,
) -> None:
    """Run summarization on an existing transcript.
    Raises if no transcript exists. Skips if a summary already exists
    (unless force). HTTP-only — no GPU."""


def process_episode(
    conn: sqlite3.Connection,
    cfg: Config,
    episode_id: int,
    *,
    force: bool = False,
    backend: str | None = None,
    model: str | None = None,
) -> None:
    """Convenience: transcribe_episode + summarize_episode.
    Used by `podracer process <id>` for one-shot CLI runs."""
```

`cmd_process` becomes a thin wrapper calling `process_episode`. The worker calls `transcribe_episode` or `summarize_episode` directly based on `job.kind`.

## DB API Additions (`podracer/db.py`)

```python
def init_worker_watermark(conn) -> str:
    """Set watermark to now() if not set. Return current watermark."""

def get_worker_watermark(conn) -> str:
    """Read watermark from config table."""

def set_worker_watermark(conn, iso_ts: str) -> None:
    """Write watermark to config table."""

def enqueue_episode_pipeline(conn, episode_id: int) -> tuple[int, int] | None:
    """INSERT a transcribe job, then a summarize job depending on it.
       Returns (transcribe_job_id, summarize_job_id), or None if either
       kind is already active for this episode (idx_jobs_active_unique)."""

def find_new_episodes_since(conn, watermark: str) -> list[int]:
    """Return episode ids where created_at > watermark
       AND podcast.subscribed = 1
       AND no active (queued/running) job exists for that episode."""

def claim_next_job(conn) -> Job | None:
    """Atomically transition the oldest queued job whose dep is satisfied
       to 'running'. Sets started_at. Returns it, or None if nothing is
       claimable."""

def mark_job_done(conn, job_id: int) -> None: ...

def mark_job_failed(conn, job_id: int, error: str) -> bool:
    """Increment attempts. If attempts < max_attempts, status -> 'queued'
       (retry next iteration). Otherwise status -> 'failed' and
       cascade-block dependents. Returns True if job is now terminal."""

def cascade_block_dependents(conn, failed_job_id: int) -> int:
    """Recursively mark queued jobs whose dep chain leads to failed_job_id
       as 'blocked'. Returns count."""

def reset_running_jobs(conn) -> int:
    """Orphan recovery: requeue jobs left in 'running' state."""

def get_job_counts(conn) -> dict[str, int]:
    """For status command: {queued, running, done, failed, blocked} counts,
       both overall and per-kind."""
```

`claim_next_job` uses a single UPDATE…RETURNING (SQLite 3.35+) with a dep check so a `summarize` job is invisible until its `transcribe` dep is `done`:

```sql
UPDATE jobs
SET status = 'running', started_at = datetime('now')
WHERE id = (
    SELECT j.id FROM jobs j
    LEFT JOIN jobs d ON d.id = j.depends_on_job_id
    WHERE j.status = 'queued'
      AND (j.depends_on_job_id IS NULL OR d.status = 'done')
    ORDER BY j.created_at
    LIMIT 1
)
RETURNING id, episode_id, kind, depends_on_job_id, attempts, max_attempts;
```

## Worker Module (`podracer/worker.py`)

```python
import signal
import threading
import time
from datetime import datetime, timezone

from podracer import logger
from podracer.config import Config, load_config
from podracer.db import (get_connection, init_db, init_worker_watermark, ...)
from podracer.feed import fetch_episodes
from podracer.process import process_episode


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.shutdown = threading.Event()
        self.conn = get_connection(cfg.db_path)
        init_db(self.conn)

    def install_signal_handlers(self) -> None:
        def _stop(signum, frame):
            logger.info("received signal %d, shutting down gracefully", signum)
            self.shutdown.set()
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    def run_once(self) -> None:
        self._sync_feeds()
        self._enqueue_new()
        self._drain_queue()

    def run_forever(self) -> None:
        reset_running_jobs(self.conn)
        init_worker_watermark(self.conn)
        interval = self.cfg.sync_interval_minutes * 60
        while not self.shutdown.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("worker iteration failed")
            # Interruptible sleep
            self.shutdown.wait(timeout=interval)

    def _sync_feeds(self) -> None: ...
    def _enqueue_new(self) -> None: ...
    def _drain_queue(self) -> None:
        while not self.shutdown.is_set():
            job = claim_next_job(self.conn)
            if job is None:
                return
            try:
                self._dispatch(job)
                mark_job_done(self.conn, job.id)
            except Exception as e:
                logger.exception("job %d (%s) failed", job.id, job.kind)
                terminal = mark_job_failed(self.conn, job.id, str(e))
                if terminal:
                    cascade_block_dependents(self.conn, job.id)

    def _dispatch(self, job: Job) -> None:
        if job.kind == 'transcribe':
            transcribe_episode(self.conn, self.cfg, job.episode_id)
        elif job.kind == 'summarize':
            summarize_episode(self.conn, self.cfg, job.episode_id)
        else:
            raise ValueError(f"unknown job kind: {job.kind}")
```

## Systemd Units

Location: `~/.config/systemd/user/`

### `podracer-web.service`

```ini
[Unit]
Description=Podracer web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/code/podracer
ExecStart=%h/code/podracer/.venv/bin/python -m podracer.cli serve --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5s
# Logs go to journal via stderr
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

### `podracer-worker.service`

```ini
[Unit]
Description=Podracer sync + processing worker
After=network-online.target
Wants=network-online.target
# Worker uses the same DB as web; ordering not required (WAL handles it)

[Service]
Type=simple
WorkingDirectory=%h/code/podracer
ExecStart=%h/code/podracer/.venv/bin/python -m podracer.cli worker
Restart=on-failure
RestartSec=30s
# Allow long-running transcription to finish before SIGKILL
TimeoutStopSec=600
# Inherit user environment for CUDA, HF cache, etc.
# If GPU env vars need to be set explicitly:
# Environment=CUDA_VISIBLE_DEVICES=0
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

### Why `%h/code/podracer` is hard-coded

The current project lives at `/home/jeremiahmalina/code/podracer` and uses `./config.toml` and `./data/` resolved from CWD. Setting `WorkingDirectory` keeps that behavior intact. If we later make the install location configurable, the units become templates.

## Install / Setup

Create a script `scripts/install-systemd-user.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cp deploy/systemd/podracer-web.service     "$UNIT_DIR/"
cp deploy/systemd/podracer-worker.service  "$UNIT_DIR/"

systemctl --user daemon-reload
systemctl --user enable  podracer-web.service podracer-worker.service
systemctl --user restart podracer-web.service podracer-worker.service

# Start at boot without login (one-time, requires sudo)
if ! loginctl show-user "$USER" | grep -q 'Linger=yes'; then
    echo "Enabling linger so services start at boot..."
    sudo loginctl enable-linger "$USER"
fi

systemctl --user status podracer-web.service podracer-worker.service --no-pager
```

Unit templates live at `deploy/systemd/` in the repo, with `%h` placeholders for the home directory.

## Failure Handling

| Failure | Behavior |
|---------|----------|
| Single feed fetch fails | Logged; other feeds still sync; loop continues. |
| Stage handler (`transcribe_episode` / `summarize_episode`) raises | Job's `attempts` incremented. If < `max_attempts`, status→queued (retried next iteration). If exhausted, status→failed; descendant jobs are cascade-blocked (a failed `transcribe` blocks the dependent `summarize`). |
| Worker process crashes | systemd restarts after 30s. Orphan recovery requeues any `running` jobs. |
| Web process crashes | systemd restarts after 5s. No state to recover. |
| Network down at boot | `After=network-online.target` delays start. Feed sync errors are non-fatal once running. |
| OOM during transcription | Worker process killed by OOM killer → systemd restart → orphan recovery → job retried on next start. Likely fails again; surface via `podracer status`. |
| Disk full mid-download | `process_episode` raises; job marked failed after retries. |
| User runs `podracer process N` while N is queued | Both run the idempotent pipeline. Whichever finishes first wins; the other no-ops on existing transcript/summary. Manual run does NOT touch the jobs table; worker's job row stays accurate. |

## Observability

- **Logs**: `journalctl --user -u podracer-worker -f` (or `-u podracer-web`).
- **Status**: `podracer status` for queue state.
- **Web**: A future `/admin/jobs` page can render the same data. Out of scope for this plan.

## Implementation Sequence

1. Add `jobs` table + indexes (including `idx_jobs_active_unique` partial index) to `SCHEMA` in `db.py`; add migration in `_migrate` for existing DBs.
2. Add `[daemon]` config section to `config.py` and `config.toml`.
3. Add `Job` model to `models.py`.
4. Add DB helpers: `init_worker_watermark`, `get/set_worker_watermark`, `enqueue_episode_pipeline`, `find_new_episodes_since`, `claim_next_job` (with dep-aware SQL), `mark_job_done`, `mark_job_failed`, `cascade_block_dependents`, `reset_running_jobs`, `get_job_counts`.
5. Extract pipeline into `podracer/process.py` with `transcribe_episode`, `summarize_episode`, and `process_episode` wrapper; rewrite `cmd_process` to call the wrapper.
6. Implement `podracer/worker.py::Worker` with eager torch/whisperx import at startup, signal handlers, `run_once`, `run_forever`, and a `_dispatch` table keyed by `job.kind`.
7. Add `cmd_worker` (with `--once`) and `cmd_status` to `cli.py` with argparse entries.
8. Manual verification: subscribe to a feed, run `podracer worker --once`, confirm both `transcribe` and `summarize` jobs flow through correctly.
9. Write unit files in `deploy/systemd/`.
10. Write `scripts/install-systemd-user.sh`.
11. Document in `docs/configuration.md`.

## Verification

1. `.venv/bin/ruff check podracer/` and `.venv/bin/ty check podracer/` pass.
2. Subscribe to a feed, then run `podracer worker --once`:
   - Confirm watermark was set, no jobs queued (because feed predates watermark).
3. Reset watermark to 1 hour ago; run `podracer worker --once`:
   - Confirm new episodes from the last hour got enqueued and processed.
4. Run `podracer status` between/after iterations; verify counts match.
5. Install units: `bash scripts/install-systemd-user.sh`.
6. `systemctl --user status podracer-{web,worker}` shows both active.
7. `journalctl --user -u podracer-worker -f` shows the loop running.
8. Stop worker mid-job (`systemctl --user stop podracer-worker`); confirm graceful exit within `TimeoutStopSec`. Restart; confirm orphan recovery requeues the in-flight job.
9. Reboot the machine; confirm both services come up under `loginctl enable-linger`.
10. Browse the web UI at `http://<host>:8080`; confirm new auto-processed episodes appear with their summaries.

## Files to Modify

- `pyproject.toml` — no new deps (everything we need is already there).
- `config.toml` — add `[daemon]` section with defaults.
- `podracer/config.py` — `Config` fields + env overrides.
- `podracer/db.py` — `jobs` table schema, migration, helper functions.
- `podracer/models.py` — `Job` model.
- `podracer/cli.py` — `cmd_worker`, `cmd_status`, argparse entries; `cmd_process` calls `process_episode`.
- `docs/configuration.md` — document daemon section + systemd install.
- `docs/plans/overview.md` — flip Phase 3 status to in-progress, link to this plan.

## Files to Create

- `podracer/process.py` — extracted pipeline function.
- `podracer/worker.py` — `Worker` class.
- `deploy/systemd/podracer-web.service`
- `deploy/systemd/podracer-worker.service`
- `scripts/install-systemd-user.sh`

## Existing Code to Reuse

- `podracer/cli.py::cmd_process` body → `process_episode`.
- `podracer/cli.py::_sync_episodes` → worker's `_sync_feeds`.
- `podracer/feed.py::fetch_episodes` — feed parsing.
- `podracer/download.py`, `podracer/transcribe.py`, `podracer/summarize.py` — pipeline stages, unchanged.
- `podracer/db.py` — connection, init, existing helpers.

## Out of Scope (Future)

- **Transcription as a model server** (`podracer-whisper.service`). Extract whisperx behind an HTTP API analogous to Ollama/vLLM so the worker stops holding the GPU itself. Unlocks concurrent job execution (multiple `summarize` jobs while a `transcribe` is in flight), removes torch from the worker, and lets web and worker run on machines without a GPU. The two-kind job model in this plan is chosen to make this a handler-side change, not a queue redesign. Deserves its own plan doc.
- **Concurrency in the worker**: once transcription is HTTP, run a small pool of in-flight jobs (e.g., 1 transcribe + N summarize concurrently). Adds asyncio or a thread pool to the drain loop.
- **Multi-GPU**: templated `podracer-worker@N.service` units pinned per `CUDA_VISIBLE_DEVICES`, with job partitioning. Only relevant once transcription is heavy or stays in-process.
- **Per-stage download job**: keeping download bundled into `transcribe_episode` is simpler. Splitting it out would let downloads continue while a transcription holds the GPU.
- **Web-driven enqueue**: a "process this episode" button in the UI that inserts directly into `jobs`. Trivial follow-up once the queue exists.
- **Backfill command**: `podracer enqueue-pending --podcast N` to bulk-enqueue old pending episodes when the user actively wants the backlog processed.
- **Backpressure on disk**: pause downloads when `media_dir` is over a threshold.
- **Metrics endpoint**: Prometheus `/metrics` on the web service exposing queue counts.
