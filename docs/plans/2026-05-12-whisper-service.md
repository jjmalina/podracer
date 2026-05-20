# Whisper Transcription Service

**Date:** 2026-05-12
**Status:** Deferred — implement once the RTX 4090 GPU host is online.
**Relates to:** [overview.md](overview.md) inference architecture, [systemd daemon](2026-05-12-systemd-daemon.md), [Deepgram backend](2026-05-18-deepgram-backend.md)

## Why deferred (2026-05-18 update)

V1 deployment runs on a CPU-only LXC and uses the [Deepgram backend](2026-05-18-deepgram-backend.md) for transcription. With no local GPU in the runtime path, there is no GPU for the worker to "hold" and nothing to extract behind an HTTP service.

This plan becomes the right move when:
- The GPU host (RTX 4090) is racked and addressable on the network.
- We want to stop paying Deepgram per-minute fees and self-host transcription on hardware we already own.
- Or we have a quality/latency reason to use whisperx + pyannote diarization specifically.

When that day comes, this plan stands as-written. The only delta is that the worker config flips from `transcribe_backend = "deepgram"` to `transcribe_backend = "whisperx-http"` pointing at the GPU host's `:9000`. Deepgram stays available as a configured fallback.

## Goal

Extract whisperx + pyannote diarization from in-process worker code into a long-running HTTP service (`podracer-whisper.service`) that holds the GPU and serves transcription requests. Mirrors the architecture we already use for summarization (Ollama / vLLM / OpenRouter behind HTTP).

After this lands, `podracer.transcribe.transcribe()` becomes a thin HTTP client. The worker no longer imports torch. Worker restart cost drops to near-zero. The drain loop can run jobs concurrently because no stage holds GPU memory in the worker.

## Why a Separate Plan

This change is orthogonal to the daemon plan. The daemon's two-kind job model (`transcribe` + `summarize`) is designed so the swap from in-process to HTTP is a handler-side change with no queue rework — so we can land either plan first.

| Order | Trade-off |
|-------|-----------|
| Daemon first, then service | Ship automation sooner. Worker holds GPU in v1; refactored to client in v2. |
| Service first, then daemon | Worker is clean from day 1, daemon ships with concurrent drain unlocked. Adds ~1 plan worth of work before automation is usable. |
| Both in parallel | More moving pieces in flight. Higher chance of integration friction. |

Recommendation in the daemon plan is **daemon first**; this doc spec'd standalone so the order is reversible.

## Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Protocol | Custom JSON over HTTP | OpenAI-compatible whisper endpoints don't include diarization. Custom JSON is simple and we control both ends. Interop with OpenAI/Groq is a future backend, not this service's surface. |
| Sync vs async | Sync blocking | Worker on same machine, single tenant. HTTP is a function-call boundary. Generous client/server timeouts (≥60 min). No client polling logic. No server-side job state — the daemon already has that. |
| Audio handover | **Multipart upload over HTTP** (revised 2026-05-18) | The service runs on the GPU host; the worker runs on the podracer LXC. No shared filesystem. Worker POSTs the audio file as multipart; the service streams it to a tempfile, transcribes, deletes. The original "file path under allowed root" design only made sense single-host. We can still support path-mode as an optional fast path if the two ever end up on the same machine. |
| Concurrency | Single in-flight request | GPU is one resource; whisperx is not thread-safe across requests. One uvicorn worker, an `asyncio.Lock` (or thread lock) around the transcription call. Subsequent requests queue server-side. |
| Model loading | Eager at startup, held for life of process | Whisper + diarization + English alignment loaded on boot. Other-language alignment models lazy-loaded into an in-memory cache. Service restart = model reload (the same 10–30 s cost the worker pays today, now isolated). |
| Model selection | Service is configured with one whisper size at startup | Multi-model serving adds VRAM budget + swap policy. Out of scope. |
| HF token | Service-side only | Service config holds the diarization token. Clients say `"diarize": true`; they don't carry credentials. |
| Service location | **GPU host, bind to LAN IP** (revised 2026-05-18) | Worker LXC is on the CPU host; whisper service is on the GPU host. Bind to the host's LAN IP. LAN is the trust boundary; simple shared-secret auth header (`Authorization: Bearer <token>`) is enough. TLS termination via the homelab reverse proxy if/when we add one. |

## Architecture

```
       ┌────────────────────────────────────────────────────────┐
       │                  SQLite (WAL)                           │
       │       podcasts │ episodes │ jobs │ config               │
       └──────▲────────────────▲──────────────────────▲─────────┘
              │                │                      │
   ┌──────────┴──────┐  ┌──────┴──────┐    ┌──────────┴────────┐
   │ podracer-web    │  │ podracer-   │    │  podracer CLI     │
   │ .service        │  │ worker      │    │  (interactive)    │
   │                 │  │ .service    │    │                   │
   │ FastAPI :8080   │  │  scheduler  │    │  manual runs      │
   │ (no torch)      │  │  + queue    │    └───────────────────┘
   └─────────────────┘  │  (no torch) │
                        └──────┬──────┘
                               │ HTTP POST /transcribe
                               ▼
                        ┌─────────────────────┐
                        │  podracer-whisper   │
                        │  .service           │
                        │                     │
                        │  FastAPI :9000      │
                        │  whisperx +         │
                        │  pyannote           │
                        │  (holds GPU)        │
                        └──────────┬──────────┘
                                   │
                            ┌──────┴──────┐
                            │     GPU     │
                            │   (VRAM     │
                            │    held)    │
                            └─────────────┘
```

## API

All endpoints are JSON. Base URL defaults to `http://127.0.0.1:9000`.

### `POST /v1/transcribe`

```json
Request:
{
  "audio_path": "/abs/path/to/episode.mp3",
  "diarize": true,
  "language": null
}
```

Fields:
- `audio_path` (required) — absolute path on the server's filesystem. Must resolve under an allowed root.
- `diarize` (optional, default true) — run pyannote speaker diarization after alignment.
- `language` (optional) — ISO 639-1 hint. If omitted, whisper auto-detects.

```json
Response 200:
{
  "text": "[00:00:01] [SPEAKER_00] Welcome to the show...\n[00:00:08] [SPEAKER_01] Glad to be here...",
  "language": "en",
  "model": "small",
  "duration_seconds": 5234.7,
  "diarized": true
}
```

```json
Response 400:
{ "error": "audio_not_found", "message": "no file at /tmp/missing.mp3" }
Response 403:
{ "error": "audio_path_disallowed", "message": "path is outside allowed roots" }
Response 503:
{ "error": "busy", "message": "another transcription is in flight" }   # only if we add a queue cap
Response 500:
{ "error": "internal", "message": "...", "request_id": "uuid4" }
Response 507:
{ "error": "oom", "message": "CUDA out of memory" }
```

The response is intentionally close to what `transcribe()` currently returns (timestamped, diarized text), so the worker's downstream code (summarization) is unchanged.

### `GET /v1/health`

```json
{
  "status": "ok",
  "model": "small",
  "device": "cuda",
  "compute_type": "float16",
  "diarize_available": true,
  "models_loaded": ["whisper:small", "align:en", "diarize:pyannote-3.1"],
  "in_flight": 0
}
```

Used by the worker's startup check and by `podracer status`.

### `GET /v1/info`

```json
{
  "version": "0.1.0",
  "uptime_seconds": 12345,
  "requests_total": 87,
  "requests_failed": 2,
  "last_error": "...",
  "last_request_at": "2026-05-12T18:32:11Z"
}
```

Lightweight observability. Not Prometheus-grade; Prometheus comes later if needed.

## Module Structure

```
podracer/whisper_service/
  __init__.py
  app.py            # FastAPI factory, lifespan loads models, routes mounted here
  state.py          # ServiceState: model handles, lock, allowed roots, stats
  routes.py         # /v1/transcribe, /v1/health, /v1/info
  __main__.py       # python -m podracer.whisper_service (uvicorn runner)
```

`podracer.whisper_service.app:create_app(cfg)` follows the same pattern as `podracer.web.app:create_app`.

### `state.py` sketch

```python
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class ServiceState:
    cfg: WhisperServiceConfig
    whisper_model: object              # whisperx model
    diarize_pipeline: object | None    # None if no HF token
    align_cache: dict[str, tuple]      # lang -> (model, metadata)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    allowed_roots: list[Path] = field(default_factory=list)
    # stats
    requests_total: int = 0
    requests_failed: int = 0
    last_error: str | None = None
    last_request_at: str | None = None
```

### Transcription handler sketch

```python
@router.post("/v1/transcribe")
async def transcribe(req: TranscribeRequest, state: ServiceState = Depends(get_state)):
    audio_path = validate_audio_path(req.audio_path, state.allowed_roots)
    if not audio_path.exists():
        raise HTTPException(400, {"error": "audio_not_found", "message": ...})

    async with state.lock:
        try:
            # Run the blocking transcription in a worker thread so the event
            # loop stays responsive for /health checks while a job is running.
            result = await asyncio.to_thread(
                _run_transcription,
                state, str(audio_path), req.diarize, req.language,
            )
            state.requests_total += 1
            state.last_request_at = utcnow_iso()
            return result
        except torch.cuda.OutOfMemoryError as e:
            state.requests_failed += 1
            state.last_error = str(e)
            raise HTTPException(507, {"error": "oom", "message": str(e)})
        except Exception as e:
            state.requests_failed += 1
            state.last_error = str(e)
            raise HTTPException(500, {"error": "internal", "message": str(e)})
```

`_run_transcription` is essentially today's `transcribe()` function, refactored to take pre-loaded model handles from `ServiceState` instead of loading them per call.

## Path Validation

The audio path comes from the worker (trusted) but we still validate, both for defense-in-depth and to make accidental misconfiguration visible:

```python
def validate_audio_path(raw: str, allowed_roots: list[Path]) -> Path:
    p = Path(raw).resolve()
    for root in allowed_roots:
        try:
            p.relative_to(root.resolve())
            return p
        except ValueError:
            continue
    raise HTTPException(403, {"error": "audio_path_disallowed", "message": str(p)})
```

`allowed_roots` defaults to `[cfg.media_dir]` and can be extended via config.

## Config

Add to `config.toml`:

```toml
[whisper_service]
host = "127.0.0.1"
port = 9000
model = "small"
device = "cuda"
compute_type = "float16"
diarize = true                       # if true, requires hf_token
preload_languages = ["en"]           # alignment models loaded at startup
allowed_roots = []                   # in addition to media_dir
# hf_token comes from [keys] section / .credentials / env (already wired)

[transcribe]
backend = "http"                     # 'local' | 'http'
service_url = "http://127.0.0.1:9000"
service_timeout_seconds = 3600
# model/device/compute_type only used when backend = 'local'
model = "small"
device = "cuda"
compute_type = "float16"
diarize = true
```

The `[transcribe]` section gains a `backend` knob so a developer can still run `local` for one-shot CLI use without the service running.

`Config` (Python) gains:

```python
# whisper service
whisper_service_host: str = "127.0.0.1"
whisper_service_port: int = 9000
whisper_service_model: str = "small"
whisper_service_device: str = "cuda"
whisper_service_compute_type: str = "float16"
whisper_service_diarize: bool = True
whisper_service_preload_languages: list[str] = field(default_factory=lambda: ["en"])
whisper_service_allowed_roots: list[str] = field(default_factory=list)

# transcribe client
transcribe_backend: str = "local"     # 'local' default keeps CLI ergonomic
transcribe_service_url: str = "http://127.0.0.1:9000"
transcribe_service_timeout_seconds: int = 3600
```

Env overrides: `PODRACER_WHISPER_SERVICE_URL`, `PODRACER_TRANSCRIBE_BACKEND`.

## Client Refactor: Transcribe Backend

Mirror the summarize.py `Backend` pattern.

```python
# podracer/transcribe.py

from dataclasses import dataclass

@dataclass
class TranscribeBackend:
    name: str   # 'local' | 'http'

    # local
    model: str = "small"
    device: str = "cuda"
    compute_type: str = "float16"

    # http
    service_url: str = "http://127.0.0.1:9000"
    timeout_seconds: int = 3600

    @staticmethod
    def local(model: str, device: str, compute_type: str) -> "TranscribeBackend":
        return TranscribeBackend(name="local", model=model, device=device,
                                  compute_type=compute_type)

    @staticmethod
    def http(service_url: str, timeout_seconds: int = 3600) -> "TranscribeBackend":
        return TranscribeBackend(name="http", service_url=service_url,
                                  timeout_seconds=timeout_seconds)


def transcribe(
    audio_path: str,
    backend: TranscribeBackend,
    *,
    diarize: bool = True,
    hf_token: str | None = None,
    language: str | None = None,
) -> str:
    if backend.name == "local":
        return _transcribe_local(audio_path, backend, diarize, hf_token, language)
    elif backend.name == "http":
        return _transcribe_http(audio_path, backend, diarize, language)
    raise ValueError(f"unknown backend: {backend.name}")


def _transcribe_local(...): ...   # current implementation, unchanged

def _transcribe_http(audio_path, backend, diarize, language) -> str:
    with httpx.Client(timeout=backend.timeout_seconds) as client:
        r = client.post(
            f"{backend.service_url}/v1/transcribe",
            json={"audio_path": audio_path, "diarize": diarize, "language": language},
        )
        r.raise_for_status()
        return r.json()["text"]
```

The `torch` / `whisperx` imports stay inside `_transcribe_local` so the http path doesn't pay them. The worker, when configured with `backend = "http"`, never triggers those imports.

`process.py::transcribe_episode` is unchanged in shape — it calls `transcribe(audio_path, backend=...)` and writes the returned text to the DB exactly as before.

## CLI Changes

### New: `podracer whisper-serve`

Long-running service. This is what `podracer-whisper.service` invokes.

```
$ podracer whisper-serve [--host 127.0.0.1] [--port 9000] [--reload]
```

Implementation calls `uvicorn.run("podracer.whisper_service.app:app", ...)` analogous to `cmd_serve` for the web UI.

### Modified: `podracer transcribe`

Adds `--backend {local,http}` and `--service-url` flags. Defaults from config. Local stays the default for ergonomic one-off use.

### `podracer status` (from daemon plan)

Extended to include a "Whisper service" section:

```
Whisper service:  http://127.0.0.1:9000  reachable=yes  model=small  in_flight=1
```

`reachable` from a 1 s timeout `/v1/health` probe.

## Installation

The whisper service is **a separate install** from the main podracer package.
This matters because torch + whisperx pull ~3 GB of CUDA libraries and only
make sense on a host with a usable GPU. CPU-only deployments (the podracer LXC
running web + worker) must not need them.

### Package layout

In `pyproject.toml`, whisperx is in an optional dependency group:

```toml
dependencies = [
    "pydantic>=2.0",
    "httpx>=0.27",
    # ... lightweight client deps only
    "deepgram-sdk>=3.0",
    "python-multipart>=0.0.9",   # for FastAPI multipart upload
]

[project.optional-dependencies]
whisper = [
    "whisperx>=3.1.0",   # pulls torch, pyannote, ctranslate2, etc.
]

[project.scripts]
podracer = "podracer.cli:main"
podracer-whisper = "podracer.whisper_service.__main__:main"
```

The `podracer.whisper_service` package is the **only** module in the codebase
that imports `torch` or `whisperx`. Nothing else in the project knows they
exist — the main CLI, web app, and worker stay slim.

### Install matrix

| Host | Install command | Pulls torch? | Runs whisper service? |
|---|---|---|---|
| GPU host (RTX 4090) | `uv pip install -e ".[whisper]"` | yes | yes |
| CPU LXC (web + worker) | `uv pip install -e .` | **no** | no |
| Dev box (full local stack) | `uv pip install -e ".[whisper]"` | yes | yes |

The `uv sync` lockfile resolution is identical between the two install
shapes — adding `--extra whisper` strictly adds packages, never changes the
base set.

### Running it

```bash
# Foreground (manual test)
python -m podracer.whisper_service --host 0.0.0.0 --port 9000

# Or via the installed script
podracer-whisper --host 0.0.0.0 --port 9000

# Or via systemd (below)
```

The process reads `config.toml` for `[whisper_service]` (host/port/auth_token)
and `[transcribe]` (whisperx_model / device / compute_type / diarize). The HF
token for diarization comes from `[keys]`, `.credentials/hf_token`, or the
`HF_TOKEN` env var — same loader as the rest of podracer.

### Failure to import torch

If someone runs `podracer-whisper` on a host where the whisper extra wasn't
installed, the entry-point's top-level `from podracer.whisper_service.app
import create_app` raises `ModuleNotFoundError: No module named 'torch'`
with a clear traceback. We don't try to be clever about this — install the
extra or don't run the service.

### Homelab deployment specifics

The Ansible role on the GPU host runs:

```yaml
- name: Install podracer with whisper extra
  pip:
    name: "."
    extras: [whisper]
    virtualenv: "{{ podracer_install_dir }}/.venv"
    chdir: "{{ podracer_install_dir }}"
```

The CPU LXC's role omits `extras`. Same repo, same Ansible code path,
different install shape — the only divergence is one boolean variable.

## Systemd Unit

`~/.config/systemd/user/podracer-whisper.service`:

```ini
[Unit]
Description=Podracer whisper transcription service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/code/podracer
ExecStart=%h/code/podracer/.venv/bin/python -m podracer.whisper_service --host 127.0.0.1 --port 9000
Restart=on-failure
RestartSec=10s
# Long enough for an in-flight transcription to finish on SIGTERM
TimeoutStopSec=900
# Pin GPU if multiple cards exist
# Environment=CUDA_VISIBLE_DEVICES=0
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

### Worker ↔ service ordering

The daemon worker doesn't strictly require the service to be up at start (it'll just fail its first transcribe job and retry). For nicer UX:

```ini
# In podracer-worker.service
After=podracer-whisper.service
Wants=podracer-whisper.service
```

`Wants=` (not `Requires=`) — worker still starts if the service fails, so it can keep enqueueing and serving `summarize` jobs.

### Install script update

Extend `scripts/install-systemd-user.sh` (from daemon plan) to also install `podracer-whisper.service`. Start order: whisper → worker. Web is independent.

## Failure Handling

| Failure | Behavior |
|---------|----------|
| Service unreachable when worker calls | httpx raises; `transcribe_episode` raises; daemon retries the job. After `max_attempts`, marks `transcribe` job failed and cascade-blocks its `summarize` dep. |
| Service returns 507 (OOM) | Same as above — retried per job. Likely indicates model too large; surface via `podracer status` and journal logs. |
| Service returns 500 | Retried per job. `request_id` in the error body lets the user grep service logs. |
| Service crashes mid-request | Worker sees connection error; treated as retryable. systemd restarts service after 10 s. |
| Service starts but model load fails | `Type=simple` reports active immediately; the lifespan handler logs the error and the process exits. systemd restart loop; eventually `RestartSec` backoff kicks in. Health endpoint reports `model_loaded=false` if we reach a partial state. |
| Worker calls service for an audio file the service can't see | 400 `audio_not_found`. Indicates `media_dir` mismatch between the two services — config bug. Non-retryable: mark failed after first attempt? Initially retry like other errors; tighten later if it becomes a real failure mode. |
| Two workers ever call the service concurrently | Second request blocks on `asyncio.Lock`. Eventually served. Subsequent SLOs are looser when more than one client exists; revisit if we add a queue cap. |

## Observability

- **Service logs**: `journalctl --user -u podracer-whisper -f`. INFO-level per request with audio path, duration, model.
- **Service stats**: `GET /v1/info`.
- **Worker view**: `podracer status` shows reachability.
- **Per-request timing**: include `duration_seconds_processed` and a wall-clock `elapsed_seconds` in the response for visibility.

## Implementation Sequence

1. Create `podracer/whisper_service/` package: `app.py` (FastAPI factory + lifespan that loads models), `state.py` (ServiceState dataclass + lock), `routes.py` (`/v1/transcribe`, `/v1/health`, `/v1/info`), `__main__.py`.
2. Move the body of today's `transcribe.py::transcribe` into `whisper_service._run_transcription`, parameterized over pre-loaded model handles in `ServiceState` instead of calling `whisperx.load_model` per call.
3. Add `[whisper_service]` and `[transcribe] backend` knobs to `config.py` and `config.toml`.
4. Refactor `podracer/transcribe.py` into `TranscribeBackend` + dispatch (`_transcribe_local`, `_transcribe_http`).
5. Update `process.py::transcribe_episode` to construct a backend from config and pass it in.
6. Add `cmd_whisper_serve` + argparse entry to `cli.py`. Extend `cmd_transcribe` with `--backend` / `--service-url`.
7. Manual smoke: `podracer whisper-serve` in one shell, `podracer transcribe <ep> --backend http` in another.
8. Write `deploy/systemd/podracer-whisper.service`.
9. Extend `scripts/install-systemd-user.sh` and the worker unit's `Wants=` line.
10. Update `docs/configuration.md` and `docs/plans/overview.md` (mention this phase as live).

## Verification

1. `ruff` + `ty` clean.
2. `podracer whisper-serve` starts and `GET /v1/health` returns `model_loaded: true`.
3. `podracer transcribe <episode_id> --backend http` produces the same transcript shape (diarized, timestamped) as the local backend would on the same audio.
4. Kill the service mid-job → client sees connection error → daemon worker retries → service restarts → second attempt succeeds.
5. Restart the worker → it imports nothing from torch (verify by inspecting startup logs / `pip-show` style import probes).
6. Subscribe to a feed and let the daemon process a new episode end-to-end via the service.
7. Run two concurrent `podracer transcribe ... --backend http` calls; verify they serialize at the service lock (second one's wall-clock starts after first one finishes).
8. Disk-share check: feed an `audio_path` outside `media_dir` → expect 403.
9. systemd restart of `podracer-whisper.service` while `podracer-worker.service` is idle → worker continues working when service returns.
10. `podracer status` reports the service as reachable and shows `model=small`.

## Files to Modify

- `pyproject.toml` — no new deps (FastAPI / uvicorn / httpx already there). Optional: pin `python-multipart` if we add upload later.
- `config.toml` — add `[whisper_service]` and `transcribe.backend` knobs.
- `podracer/config.py` — config fields + env overrides.
- `podracer/transcribe.py` — split into backend-dispatched client; keep `_transcribe_local` as the existing logic.
- `podracer/process.py` — pass a `TranscribeBackend` built from config into `transcribe()`.
- `podracer/cli.py` — `cmd_whisper_serve` + flags on `cmd_transcribe`. Extend `cmd_status` to probe `/v1/health`.
- `deploy/systemd/podracer-worker.service` — `After=` / `Wants=` whisper unit.
- `scripts/install-systemd-user.sh` — install the third unit, start in order.
- `docs/configuration.md`, `docs/plans/overview.md`.

## Files to Create

- `podracer/whisper_service/__init__.py`
- `podracer/whisper_service/app.py`
- `podracer/whisper_service/state.py`
- `podracer/whisper_service/routes.py`
- `podracer/whisper_service/__main__.py`
- `deploy/systemd/podracer-whisper.service`

## Existing Code to Reuse

- All of today's `podracer/transcribe.py` body — verbatim — inside `_run_transcription`.
- `podracer/config.py` — load patterns.
- `podracer/web/app.py` — FastAPI factory + lifespan pattern.

## Out of Scope (Future)

- **OpenAI-compatible endpoint** (`POST /v1/audio/transcriptions` multipart). Enables swap-in interop with cloud whisper providers (Groq, OpenAI) at the cost of losing diarization. Add as a parallel route if needed.
- **Multipart upload** for cross-machine deployment.
- **Multi-model serving** with on-demand swap (small/medium/large in one service).
- **Cross-request batching** — group short clips into a single whisperx call for throughput. Only useful at scale.
- **Auth / TLS** — needed if we expose the service beyond loopback.
- **Concurrent transcription on multi-GPU** — one service per GPU, behind a load balancer or a `podracer-whisper@N.service` template.
- **Prometheus `/metrics`** — fold into the same endpoint the web service might expose later.
- **Streaming transcription** — partial transcripts via SSE / WebSocket. Useful only if we add a live ingestion path.
