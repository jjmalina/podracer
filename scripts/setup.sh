#!/usr/bin/env bash
# One-shot setup for podracer on Debian/Ubuntu. Idempotent — re-run any time.
#
# Usage:
#   bash scripts/setup.sh                # CPU-only (Deepgram + OpenRouter)
#   bash scripts/setup.sh --with-whisper # also pull torch + whisperx (~3 GB,
#                                        # needs an NVIDIA GPU to actually use)
#
# After this finishes you can:
#   - Drop API keys in .credentials/ (deepgram_token, openrouter_token)
#   - Run `podracer` from anywhere
#   - Optionally `bash scripts/install-systemd-user.sh` to enable the daemon
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WITH_WHISPER=0
for arg in "$@"; do
    case "$arg" in
        --with-whisper) WITH_WHISPER=1 ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's|^# \?||'
            exit 0 ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2 ;;
    esac
done

cd "$REPO_DIR"

# ---------- OS check ----------
if [[ ! -r /etc/os-release ]]; then
    echo "error: this script targets Debian/Ubuntu; /etc/os-release not found" >&2
    exit 1
fi
. /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
    debian:*|ubuntu:*|*:*debian*|*:*ubuntu*) ;;
    *)
        echo "warning: untested on '$ID' (${ID_LIKE:-no ID_LIKE}); proceeding anyway" >&2
        ;;
esac

# ---------- System packages ----------
APT_PKGS=(python3 python3-venv ffmpeg git ca-certificates curl)
need_apt=()
for pkg in "${APT_PKGS[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        need_apt+=("$pkg")
    fi
done

if (( ${#need_apt[@]} > 0 )); then
    echo "==> Installing system packages: ${need_apt[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${need_apt[@]}"
else
    echo "==> System packages already installed"
fi

# ---------- uv ----------
if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Pick up uv on this shell's PATH for the rest of the script.
    # The installer adds it to ~/.cargo/bin or ~/.local/bin depending on version.
    if [[ -x "$HOME/.local/bin/uv" ]]; then export PATH="$HOME/.local/bin:$PATH"; fi
    if [[ -x "$HOME/.cargo/bin/uv" ]]; then export PATH="$HOME/.cargo/bin:$PATH"; fi
fi
echo "==> uv: $(uv --version)"

# ---------- venv + deps ----------
if (( WITH_WHISPER == 1 )); then
    echo "==> uv sync --extra whisper (pulls torch + whisperx; ~3 GB)"
    uv sync --extra whisper
else
    echo "==> uv sync (slim install; no torch/whisperx)"
    uv sync
fi

# ---------- PATH symlinks ----------
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_DIR/.venv/bin/podracer"          "$HOME/.local/bin/podracer"
if [[ -x "$REPO_DIR/.venv/bin/podracer-whisper" ]]; then
    ln -sf "$REPO_DIR/.venv/bin/podracer-whisper" "$HOME/.local/bin/podracer-whisper"
fi

if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
    echo "warning: $HOME/.local/bin is not on your PATH. Add this to ~/.bashrc or ~/.zshrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# ---------- .credentials placeholder ----------
mkdir -p "$REPO_DIR/.credentials"
if [[ ! -e "$REPO_DIR/.credentials/example" ]]; then
    cat > "$REPO_DIR/.credentials/example" <<'EOF'
# Drop one token per file in this directory (no extension):
#   .credentials/deepgram_token       <- Deepgram API key
#   .credentials/openrouter_token     <- OpenRouter API key
#   .credentials/hf_token             <- HuggingFace token (for whisperx
#                                       diarization, only if using
#                                       --backend whisperx-http)
# Each file: chmod 600. They are gitignored.
EOF
fi

# ---------- Next-steps banner ----------
cat <<EOF

============================================================
Setup complete.

Next steps:
  1. Add API keys (any subset):
       echo 'YOUR_DEEPGRAM_KEY'   > .credentials/deepgram_token
       echo 'YOUR_OPENROUTER_KEY' > .credentials/openrouter_token

  2. Subscribe to a podcast and process an episode:
       podracer subscribe https://feeds.example.com/feed.xml
       podracer episodes <podcast_id>
       podracer process <episode_id>

  3. (Optional) Run the web UI:
       podracer serve --host 0.0.0.0 --port 8080

  4. (Optional) Install systemd --user services for auto-sync + processing:
       bash scripts/install-systemd-user.sh

Docs: README.md, docs/configuration.md
============================================================
EOF
