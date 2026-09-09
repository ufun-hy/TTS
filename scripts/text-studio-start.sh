#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${TEXT_STUDIO_HOST:-127.0.0.1}"
PORT="${TEXT_STUDIO_PORT:-8770}"
exec python3 "$ROOT/server/text_studio.py" --host "$HOST" --port "$PORT"
