#!/bin/bash
#
# WhisperDrop — self-contained launcher.
# First run: downloads uv, builds a private Python env, installs dependencies.
# Every run after: launches instantly, fully offline.
#
# Double-click this file in Finder. (First time: right-click → Open to get
# past macOS Gatekeeper, then click Open.)

set -e

# Always work relative to this script's own folder, wherever it was copied to.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

BIN="$HERE/bin"
UV="$BIN/uv"
VENV="$HERE/.venv"
PY="$VENV/bin/python"

log() { printf "\033[1;35m[WhisperDrop]\033[0m %s\n" "$1"; }

# ── 1. Ensure uv (self-contained Python/dependency manager) ──────────────────
if [ ! -x "$UV" ]; then
  log "First-time setup — installing the environment manager (uv)…"
  mkdir -p "$BIN"
  # Install uv into ./bin without touching the rest of the system.
  export UV_INSTALL_DIR="$BIN"
  export UV_UNMANAGED_INSTALL="$BIN"
  curl -LsSf https://astral.sh/uv/install.sh | env INSTALLER_NO_MODIFY_PATH=1 sh
  # The installer may place it directly in $BIN; make sure it's there.
  if [ ! -x "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then
    cp "$HOME/.local/bin/uv" "$UV"
  fi
fi

# ── 2. Ensure the virtual environment + dependencies ─────────────────────────
if [ ! -x "$PY" ]; then
  log "Setting up Python 3.11 (one-time)…"
  "$UV" venv --python 3.11 "$VENV"
fi

# Marker so we only run the (slow) install once.
if [ ! -f "$HERE/.deps_installed" ]; then
  log "Downloading dependencies — this is a large, one-time download (~2GB). Please wait…"
  "$UV" pip install --python "$PY" openai-whisper tkinterdnd2 static-ffmpeg
  # Pre-fetch the static ffmpeg binary now so first transcription isn't delayed.
  "$PY" -c "import static_ffmpeg; static_ffmpeg.add_paths()" || true
  touch "$HERE/.deps_installed"
  log "Setup complete."
fi

# ── 3. Launch the app ────────────────────────────────────────────────────────
log "Starting WhisperDrop…"
exec "$PY" "$HERE/app.py"
