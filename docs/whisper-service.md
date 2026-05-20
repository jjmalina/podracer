# Whisper transcription service

The whisper service is a long-running HTTP wrapper around WhisperX +
pyannote diarization. It holds models in memory so each request reuses the
loaded weights instead of paying the 10–30 s warm-up cost.

The rest of podracer (web, worker, CLI) talks to it over HTTP. Nothing else
in the codebase imports `torch` or `whisperx` — they live exclusively in the
`podracer.whisper_service` package.

## Install

torch and whisperx are an **optional install**. The main `pip install -e .`
gives you the slim client + Deepgram path with no CUDA libs. To run the
whisper service:

```bash
uv sync --extra whisper
```

This adds `whisperx` (and transitively torch, pyannote, ctranslate2, ~3 GB of
CUDA libs). The CPU LXC running web + worker should NOT install this extra.

## Configure

In `config.toml`:

```toml
[transcribe]
backend = "whisperx-http"
service_url = "http://127.0.0.1:9000"
# service_auth_token = "..."     # if the service requires bearer auth
diarize = true

[whisper_service]
host = "127.0.0.1"               # bind 0.0.0.0 for cross-host access
port = 9000
# auth_token = "..."             # set to require Bearer auth on /v1/transcribe

# Server-side model selection (used when the service starts)
# These also live under [transcribe]:
#   whisperx_model = "small"     # tiny / base / small / medium / large
#   device         = "cuda"
#   compute_type   = "float16"   # int8 if running on CPU

[keys]
hf_token = "hf_..."              # required for diarization
```

Diarization needs a HuggingFace token with accepted licenses for
[pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
and [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0).

## Run

```bash
.venv/bin/python -m podracer.whisper_service
```

Look for these log lines:

```
Loading whisper model: small (device=cuda, compute=float16)
Loading diarization pipeline
Whisper service ready (model=small, device=cuda, diarize=True, auth=no)
Uvicorn running on http://127.0.0.1:9000
```

Model load runs in the FastAPI lifespan, so the service won't accept
connections until the model is on the GPU.

## Endpoints

### `GET /v1/health`

```json
{
  "status": "ok",
  "model": "small",
  "device": "cuda",
  "compute_type": "float16",
  "diarize_available": true,
  "in_flight": 0
}
```

### `GET /v1/info`

Same fields as health, plus request counts, last error, and loaded alignment
languages.

### `POST /v1/transcribe`

Multipart form-data:

| Field | Required | Notes |
|---|---|---|
| `audio` | yes | The audio file. Streamed to a tempfile, deleted after the call. |
| `diarize` | no | `"true"` / `"false"`. Defaults to true. Returns 400 if true but the service has no HF token. |
| `language` | no | ISO 639-1 hint (e.g. `"en"`). Omit for auto-detection. |

Returns:

```json
{
  "text": "[00:00:01] [SPEAKER_00] Welcome to the show...\n...",
  "language": "en",
  "model": "small",
  "diarized": true,
  "elapsed_seconds": 124.6
}
```

The transcript format matches what the rest of podracer expects from any
transcription backend: `[HH:MM:SS] [SPEAKER_XX] text`.

## Testing

End-to-end smoke test in two terminals.

### Terminal 1 — service

```bash
cd ~/code/podracer
uv sync --extra whisper          # one time
.venv/bin/python -m podracer.whisper_service
```

### Terminal 2 — client

```bash
cd ~/code/podracer

# Health
curl -s http://127.0.0.1:9000/v1/health | python -m json.tool

# Cut a 60s clip from any episode you have
ffmpeg -y -loglevel error \
  -i data/media/<podcast>/<episode>.mp3 \
  -t 60 /tmp/clip.mp3

# Direct HTTP — multipart upload
curl -s -X POST http://127.0.0.1:9000/v1/transcribe \
  -F "audio=@/tmp/clip.mp3" \
  -F "diarize=true" | python -m json.tool

# Python client
.venv/bin/python -c "
from podracer.transcribe import transcribe
print(transcribe('/tmp/clip.mp3',
                 backend='whisperx-http',
                 service_url='http://127.0.0.1:9000',
                 diarize=True))
"

# Full pipeline through the service (set config.toml: backend = 'whisperx-http')
.venv/bin/python -m podracer.cli process <episode_id> --force
```

### Auth check

If you set `auth_token` in `[whisper_service]`:

```bash
# 401 — no header
curl -i -X POST http://127.0.0.1:9000/v1/transcribe -F "audio=@/tmp/clip.mp3"

# 403 — wrong token
curl -i -X POST http://127.0.0.1:9000/v1/transcribe -F "audio=@/tmp/clip.mp3" \
  -H "Authorization: Bearer wrong"

# 200 — right token
curl -i -X POST http://127.0.0.1:9000/v1/transcribe -F "audio=@/tmp/clip.mp3" \
  -H "Authorization: Bearer <your-token>"
```

### Concurrency

The service serializes transcription with an asyncio lock so two concurrent
clients won't compete for the GPU. Verify by running two `curl` calls in
parallel and watching `/v1/info` — `in_flight` toggles between 0 and 1, never
2. The second request blocks until the first finishes.

## Operational notes

- **Restart cost.** Model reload on startup is ~10–30 s. Plan systemd
  `TimeoutStopSec` and `RestartSec` around this.
- **OOM.** GPU OOM during a request returns 500 with the error message. The
  process stays up; the worker retries the job per its retry policy.
- **Audio handover.** Multipart upload means the service does not need access
  to the worker's filesystem. A 200 MB MP3 streams to a tempfile under
  `/tmp/whisper-*.mp3`, transcribes, and gets deleted in the request's
  `finally` block.
- **Languages.** Whisper auto-detects on first pass. Alignment models load
  per language on first request and stay cached in memory for the life of
  the process. The `/v1/info` endpoint shows which alignment models are
  loaded.

## Systemd

For long-running operation, see the
[whisper service plan](plans/2026-05-12-whisper-service.md#systemd-unit) for
the unit file.
