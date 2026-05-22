# podracer

Local-first podcast knowledge platform. Subscribes to RSS feeds, downloads
episodes, transcribes them with speaker diarization, and summarizes them
with an LLM. Browse the result in a local web UI or query the SQLite
database directly.

For the longer "why" and target architecture, see
[docs/README.md](docs/README.md).

## Requirements

- Python 3.10–3.13
- [uv](https://docs.astral.sh/uv/) for dependency management
- One transcription backend:
  - **Deepgram** (cloud, default) — no GPU needed, ~$0.0043/min
  - **whisperx-http** (self-hosted) — needs an NVIDIA GPU and the `whisper`
    extra; see [docs/whisper-service.md](docs/whisper-service.md)
- One summarization backend:
  - **OpenRouter** (cloud, default) — pay-per-use
  - Or a local Ollama / vLLM running an OpenAI-compatible endpoint

## Install

Debian/Ubuntu:

```bash
git clone https://github.com/jjmalina/podracer.git
cd podracer

# Foundational install: apt packages, uv, venv, symlinks into ~/.local/bin.
# Slim by default — no torch/whisperx.
bash scripts/setup.sh

# To also pull torch + whisperx for the local whisper service (~3 GB, GPU):
bash scripts/setup.sh --with-whisper
```

The script is idempotent — safe to re-run after `git pull` or to add the
whisper extra later.

### Manual install (other distros, or if you don't want the script)

```bash
# Install uv however you prefer: https://docs.astral.sh/uv/getting-started/installation/

uv sync                  # slim
# OR: uv sync --extra whisper

# Put the entry-point scripts on PATH (shebang hard-codes the venv's Python)
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/podracer"         ~/.local/bin/podracer
ln -sf "$PWD/.venv/bin/podracer-whisper" ~/.local/bin/podracer-whisper
```

### Credentials

Drop API tokens in `.credentials/` (gitignored) or set env vars:

```bash
mkdir -p .credentials
echo "<deepgram-key>" > .credentials/deepgram_token
echo "<openrouter-key>" > .credentials/openrouter_token
# For whisperx diarization only:
echo "<huggingface-token>" > .credentials/hf_token
```

See [docs/configuration.md](docs/configuration.md) for the full layered
config (TOML → credentials files → env vars → CLI flags).

## Quick start

```bash
# Discover a podcast
podracer search "Huberman Lab"

# Subscribe by RSS feed URL
podracer subscribe https://feeds.megaphone.fm/hubermanlab

# Browse episodes
podracer episodes <podcast_id>

# One-shot pipeline: download → transcribe → summarize
podracer process <episode_id>

# Or run the web UI and read in a browser
podracer serve --host 0.0.0.0 --port 8080
# → http://localhost:8080
```

> **Note on database location.** `podracer` defaults to `./data/podracer.db`
> (relative to your current directory). If you want one DB regardless of
> where you invoke from, set `PODRACER_DB=/abs/path/podracer.db` in your
> environment, or set an absolute `db_path` in `config.toml`.

## Run as a daemon

For unattended operation (auto-sync feeds + process new episodes):

```bash
# One-shot dry run from the repo — sync feeds, enqueue new episodes, drain
podracer worker --once
podracer status

# Install as systemctl --user services (web + worker, restart on failure,
# starts at boot via loginctl enable-linger). Sets up XDG config + data so
# the daemons don't share state with your dev checkout.
bash scripts/install-systemd-user.sh

# Tail the logs
journalctl --user -u podracer-worker -f
journalctl --user -u podracer-web -f
```

The install script creates:
- `~/.config/podracer/config.toml` (seeded from `deploy/config.toml.template`)
- `~/.local/share/podracer/podracer.db` + `media/`
- `~/.config/podracer/.credentials/` (copied from the repo's `.credentials/`
  on first install)

**Dev vs daemon isolation.** Running `podracer` from `~/code/podracer/`
uses the in-repo `config.toml` and `data/`. Running it from anywhere else
(or via the systemd units, which have `WorkingDirectory=%h`) uses the XDG
config and data. Same binary, different state.

The worker uses a watermark — on first run it sets the watermark to "now"
so the existing backlog is NOT auto-processed. New episodes published
after that get picked up automatically. To bulk-enqueue old episodes,
roll the watermark back manually (Python REPL with
`set_worker_watermark(conn, '2020-01-01 00:00:00')`).

## CLI reference

```
podracer search <query>            # find podcasts
podracer subscribe <feed_url>      # add a subscription
podracer unsubscribe <podcast_id>
podracer list                      # show subscriptions
podracer episodes <podcast_id>     # list episodes
podracer sync [podcast_id]         # pull new episodes from feed(s)
podracer download <episode_id>     # download audio only
podracer transcribe <episode_id>   # transcribe (Deepgram or whisperx-http)
podracer summarize <episode_id>    # summarize via LLM
podracer process <episode_id>      # all of the above end-to-end
podracer serve                     # start web UI
podracer worker [--once]           # daemon loop
podracer status [--json]           # queue + watermark state
```

All commands accept `--json` and `-v/--verbose` flags.

## Architecture quick reference

```
                    SQLite (WAL)
                   ┌────────────────────────────┐
                   │ podcasts │ episodes │ jobs │
                   └─────▲──────▲──────────▲────┘
                         │      │          │
                ┌────────┴┐  ┌──┴──┐   ┌───┴───┐
                │   web   │  │ CLI │   │worker │
                │  :8080  │  │     │   │       │
                └─────────┘  └─────┘   └───┬───┘
                                           │
                  ┌────────────────────────┼───────────────────────┐
                  ▼                        ▼                       ▼
            Deepgram /            podracer-whisper                OpenRouter /
            whisperx-http         (separate process,              vLLM / Ollama
                                  optional GPU host)
```

See [docs/](docs/) for the planning documents and per-feature notes.

## Development

```bash
# Lint
.venv/bin/ruff check podracer/

# Type-check
.venv/bin/ty check podracer/

# Run the web UI with auto-reload
podracer serve --reload

# Run the whisper service (requires the `whisper` extra)
python -m podracer.whisper_service
```

See [CLAUDE.md](CLAUDE.md) for repo-specific coding rules.
