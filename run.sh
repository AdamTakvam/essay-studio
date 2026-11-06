#!/usr/bin/env bash
# Essay Studio launcher: creates a virtualenv on first run, then starts the server.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null; then
  echo "ffmpeg not found — install it first (e.g. sudo apt install ffmpeg)"; exit 1
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
.venv/bin/pip install -q -r requirements.txt

if [ -z "${ANTHROPIC_API_KEY:-}" ] && ! grep -qs '^ANTHROPIC_API_KEY=.' .env; then
  echo "Note: ANTHROPIC_API_KEY is not set (export it or put it in .env)."
fi

PORT="${PORT:-8765}"
echo "Essay Studio → http://localhost:${PORT}"
exec .venv/bin/uvicorn server.app:app --host 127.0.0.1 --port "$PORT"
