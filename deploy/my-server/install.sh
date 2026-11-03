#!/usr/bin/env bash
# Install / upgrade the transcribe service ON THE MAC SERVER. Idempotent; re-run after git pull.
#   ssh <server> 'cd ~/Projects/ham-net-speech-to-text && git pull && deploy/my-server/install.sh'
# Does: venv (python3.11) → pip → private env file with a fresh
# EMCOMM_STT_KEY (first run only) → emcomm_voice database + schema → launchd plist → /health.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3.11}"
ENV_FILE="$HOME/.config/emcomm-stt/env"
LABEL="${LABEL:-org.hamnet.emcomm-stt}"   # set LABEL in the environment to keep an existing service name
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"
cd "$REPO"

echo "→ venv + requirements"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r transcribe/requirements.txt -r analyze/requirements.txt

if [ ! -f "$ENV_FILE" ]; then
  echo "→ writing $ENV_FILE (new EMCOMM_STT_KEY — copy it to the puck's environment)"
  mkdir -p "$(dirname "$ENV_FILE")"; chmod 700 "$(dirname "$ENV_FILE")"
  umask 077
  cat > "$ENV_FILE" <<ENV
# ham-net-speech-to-text transcribe service — sourced by deploy/my-server/run.sh. Mode 600.
EMCOMM_STT_KEY=$(openssl rand -hex 24)
EMCOMM_DB_DSN=postgresql+psycopg://$USER@localhost:5432/emcomm_voice
STT_DATA_DIR=$HOME/data/emcomm-voice
STT_MODEL=mlx-community/whisper-large-v3-turbo
STT_PORT=8090
# LITELLM_URL=http://<llm-gateway>:4000/v1   # optional: analyze/ only
# LITELLM_KEY=   # per-app virtual key (LITELLM_EMCOMM_KEY on LLM server); needed by analyze/, not by transcribe/
ENV
  umask 022
fi
set -a; . "$ENV_FILE"; set +a
mkdir -p "$STT_DATA_DIR" "$HOME/Library/Logs/hamnet"

echo "→ database emcomm_voice"
pg_isready -q
psql -Atqc "select 1 from pg_database where datname='emcomm_voice'" postgres | grep -q 1 || createdb emcomm_voice
psql -q -v ON_ERROR_STOP=1 -d emcomm_voice -f db/schema.sql

echo "→ launchd $LABEL"
sed -e "s|__LABEL__|$LABEL|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  deploy/my-server/emcomm-stt.plist.template > "$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"
for i in $(seq 1 30); do curl -sf "localhost:${STT_PORT:-8090}/health" && { echo; echo "✓ up"; exit 0; }; sleep 1; done
echo "!! service did not answer /health; see ~/Library/Logs/hamnet/emcomm-stt.err" >&2; exit 1
