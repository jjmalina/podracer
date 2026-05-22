#!/usr/bin/env bash
# Install podracer-web and podracer-worker as systemctl --user services.
# Daemons read config from ~/.config/podracer/config.toml and store data
# under ~/.local/share/podracer/ — separate from the dev repo's config.toml
# and data/ directory.
#
# Idempotent: safe to re-run after pulling new code. Won't overwrite an
# existing XDG config or credentials.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/podracer"
CREDS_DIR="$CONFIG_DIR/.credentials"
DATA_DIR="$HOME/.local/share/podracer"

echo "==> Creating XDG directories"
mkdir -p "$UNIT_DIR" "$CONFIG_DIR" "$CREDS_DIR" "$DATA_DIR/media"

echo "==> Seeding config.toml"
if [[ -e "$CONFIG_DIR/config.toml" ]]; then
    echo "    $CONFIG_DIR/config.toml already exists; leaving it alone"
else
    sed "s|__HOME__|$HOME|g" "$REPO_DIR/deploy/config.toml.template" \
        > "$CONFIG_DIR/config.toml"
    echo "    wrote $CONFIG_DIR/config.toml"
fi

echo "==> Copying credentials from $REPO_DIR/.credentials/"
if [[ -d "$REPO_DIR/.credentials" ]]; then
    shopt -s nullglob
    copied=0
    for src in "$REPO_DIR/.credentials"/*; do
        name="$(basename "$src")"
        # Skip the example file
        [[ "$name" == "example" ]] && continue
        dest="$CREDS_DIR/$name"
        if [[ -e "$dest" ]]; then
            echo "    $name: already present, skipping"
        else
            cp "$src" "$dest"
            chmod 600 "$dest"
            echo "    $name: copied"
            copied=$((copied + 1))
        fi
    done
    shopt -u nullglob
    echo "    ($copied new file(s))"
else
    echo "    no .credentials/ in repo; set keys in $CONFIG_DIR/config.toml or env vars"
fi

echo "==> Installing systemd units"
cp "$REPO_DIR/deploy/systemd/podracer-web.service"    "$UNIT_DIR/"
cp "$REPO_DIR/deploy/systemd/podracer-worker.service" "$UNIT_DIR/"

systemctl --user daemon-reload
systemctl --user enable  podracer-web.service podracer-worker.service
systemctl --user restart podracer-web.service podracer-worker.service

# Start at boot without an active session (one-time, requires sudo)
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
    echo "==> Enabling linger so services start at boot (sudo required)..."
    sudo loginctl enable-linger "$USER"
fi

echo
systemctl --user status podracer-web.service podracer-worker.service --no-pager || true

cat <<EOF

Daemons installed.
  config: $CONFIG_DIR/config.toml
  data:   $DATA_DIR/
  logs:   journalctl --user -u podracer-{web,worker} -f
EOF
