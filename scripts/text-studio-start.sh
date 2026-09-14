#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${TEXT_STUDIO_HOST:-127.0.0.1}"
PORT="${TEXT_STUDIO_PORT:-8770}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "${TEXT_STUDIO_PYTHON:-python3}" "$ROOT/server/text_studio_entry.py" --host "$HOST" --port "$PORT" "$@"
