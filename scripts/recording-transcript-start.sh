#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${RECORDING_TRANSCRIPT_HOST:-127.0.0.1}"
PORT="${RECORDING_TRANSCRIPT_PORT:-8771}"
PYTHON="${ASR_PYTHON:-$ROOT/.venv-asr/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

exec "$PYTHON" "$ROOT/server/recording_transcript.py" --host "$HOST" --port "$PORT" "$@"
