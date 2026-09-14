#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${RECORDING_TRANSCRIPT_HOST:-127.0.0.1}"
PORT="${RECORDING_TRANSCRIPT_PORT:-8771}"
PYTHON="${RECORDING_TRANSCRIPT_PYTHON:-$ROOT/runtime/qwen-asr-venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Qwen ASR 环境不存在：$PYTHON；请先安装 scripts/requirements-qwen-asr.txt" >&2
  exit 1
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Inference is local-only; missing assets must fail instead of downloading.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
exec "$PYTHON" "$ROOT/server/recording_transcript.py" --host "$HOST" --port "$PORT" "$@"
