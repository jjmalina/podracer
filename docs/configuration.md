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
model = "small"          # tiny, base, small, medium, large-v3
device = "cuda"          # cuda, cpu
compute_type = "float16" # float16, int8, float32
diarize = true           # enable speaker diarization

[summarize]
backend = "ollama"       # ollama, vllm, openrouter
model = "gemma4:e4b"     # model name (backend-specific)
# base_url = "http://localhost:11434"  # override backend URL

[keys]
# hf_token = "hf_..."
# openrouter_api_key = "sk-or-..."
# podcast_index_key = "..."
# podcast_index_secret = "..."
```

## Credentials

API keys can be set in three places (checked in this order):

| Key | config.toml | .credentials/ file | Env var |
|-----|-------------|-------------------|---------|
| HuggingFace | `[keys] hf_token` | `.credentials/hf_token` | `HF_TOKEN` |
| OpenRouter | `[keys] openrouter_api_key` | `.credentials/openrouter_token` | `OPENROUTER_API_KEY` |
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
| `PODCAST_INDEX_KEY` | `keys.podcast_index_key` |
| `PODCAST_INDEX_SECRET` | `keys.podcast_index_secret` |

## CLI Flag Overrides

Most config values can be overridden per-command:

```bash
# Override transcription settings
podracer transcribe 1 --model large-v3 --device cpu --no-diarize

# Override summarization settings
podracer summarize 1 --backend openrouter --model qwen/qwen3.6-35b-a3b
```
