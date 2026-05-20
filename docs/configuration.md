# Configuration

Podracer uses a layered config system. Each layer overrides the previous:

1. **Defaults** (built into the code)
2. **`config.toml`** (project root)
3. **`.credentials/`** files (project root)
4. **Environment variables**
5. **CLI flags** (highest priority)

## config.toml

```toml
[general]
db_path = "./data/podracer.db"
media_dir = "./data/media/"

[transcribe]
backend = "deepgram"           # "deepgram" (cloud) or "whisperx-http" (self-hosted)
deepgram_model = "nova-3"      # nova-3, nova-2, etc.
whisperx_model = "small"       # tiny/base/small/medium/large (used by the whisper service)
device = "cuda"                # whisper service: cuda or cpu
compute_type = "float16"       # whisper service: float16 / int8 / float32
diarize = true
# service_url = "http://gpu-host:9000"   # for whisperx-http
# service_auth_token = "..."             # optional bearer auth

[summarize]
backend = "openrouter"             # ollama, vllm, openrouter
model = "deepseek/deepseek-v4-flash"
# base_url = "http://localhost:11434"

# Server-side config for `python -m podracer.whisper_service`
# [whisper_service]
# host = "0.0.0.0"
# port = 9000
# auth_token = "..."

[keys]
# hf_token = "hf_..."
# openrouter_api_key = "sk-or-..."
# deepgram_api_key = "..."
# podcast_index_key = "..."
# podcast_index_secret = "..."
```

See [whisper-service.md](whisper-service.md) for running the local whisper backend.

## Credentials

API keys can be set in three places (checked in this order):

| Key | config.toml | .credentials/ file | Env var |
|-----|-------------|-------------------|---------|
| HuggingFace | `[keys] hf_token` | `.credentials/hf_token` | `HF_TOKEN` |
| OpenRouter | `[keys] openrouter_api_key` | `.credentials/openrouter_token` | `OPENROUTER_API_KEY` |
| Deepgram | `[keys] deepgram_api_key` | `.credentials/deepgram_token` | `DEEPGRAM_API_KEY` |
| Podcast Index key | `[keys] podcast_index_key` | `.credentials/podcast_index` (line 1) | `PODCAST_INDEX_KEY` |
| Podcast Index secret | `[keys] podcast_index_secret` | `.credentials/podcast_index` (line 2) | `PODCAST_INDEX_SECRET` |

The `.credentials/` directory is gitignored. See `.credentials/example` for setup instructions.

## Environment Variables

| Variable | Overrides |
|----------|-----------|
| `PODRACER_DB` | `general.db_path` |
| `PODRACER_MEDIA_DIR` | `general.media_dir` |
| `HF_TOKEN` | `keys.hf_token` |
| `OPENROUTER_API_KEY` | `keys.openrouter_api_key` |
| `DEEPGRAM_API_KEY` | `keys.deepgram_api_key` |
| `PODCAST_INDEX_KEY` | `keys.podcast_index_key` |
| `PODCAST_INDEX_SECRET` | `keys.podcast_index_secret` |

## CLI Flag Overrides

Most config values can be overridden per-command:

```bash
# Override transcription settings
podracer transcribe 1 --backend deepgram --model nova-3
podracer transcribe 1 --backend whisperx-http --no-diarize

# Override summarization settings
podracer summarize 1 --backend openrouter --model deepseek/deepseek-v4-flash
```
