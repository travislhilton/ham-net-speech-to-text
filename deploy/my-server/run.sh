#!/usr/bin/env bash
# Entry point for com.example.emcomm-stt (launchd). Loads the private env file, execs uvicorn.
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV_FILE="$HOME/.config/emcomm-stt/env"
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE — run deploy/home-server/install.sh" >&2; exit 78; }
set -a; . "$ENV_FILE"; set +a
export PATH="/opt/homebrew/bin:$PATH"   # launchd PATH lacks Homebrew (ffmpeg for non-16k inputs)
mkdir -p "$HOME/Library/Logs/example" "${STT_DATA_DIR:?STT_DATA_DIR unset}"
exec .venv/bin/uvicorn transcribe.server:app --host 0.0.0.0 --port "${STT_PORT:-8090}"
