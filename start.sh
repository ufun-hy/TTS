#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$ROOT/runtime/bin"
MODEL_DIR="$ROOT/runtime/models"
LOG_DIR="$ROOT/runtime/logs"
ENGINE_PORT="${COSYVOICE_ENGINE_PORT:-8766}"
API_PORT="${TTS_PORT:-8765}"
HOST="0.0.0.0"
BACKEND="${COSYVOICE_BACKEND:-auto}"

if [[ "${1:-}" == "--lan" ]]; then
  HOST="0.0.0.0"
elif [[ "${1:-}" != "" ]]; then
  echo "usage: ./start.sh [--lan]" >&2
  exit 2
fi

required=(
  "$BIN_DIR/cosyvoice-server"
  "$MODEL_DIR/CosyVoice3-2512_Q8_0.gguf"
  "$MODEL_DIR/prompt_speech.gguf"
)
for path in "${required[@]}"; do
  if [[ ! -s "$path" ]]; then
    echo "Missing $path" >&2
    echo "Run: ./scripts/install.sh" >&2
    exit 1
  fi
done

mkdir -p "$LOG_DIR" "$ROOT/runtime/audio"
export DYLD_LIBRARY_PATH="/opt/homebrew/opt/icu4c/lib:$BIN_DIR${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

"$BIN_DIR/cosyvoice-server" \
  --model "$MODEL_DIR/CosyVoice3-2512_Q8_0.gguf" \
  --voice-prompt "alloy=$MODEL_DIR/prompt_speech.gguf" \
  --served-model-name cosyvoice-3 \
  --backend "$BACKEND" \
  --host 127.0.0.1 \
  --port "$ENGINE_PORT" \
  --concurrency 1 \
  --verbose \
  --api \
  >"$LOG_DIR/cosyvoice-server.log" 2>&1 &
ENGINE_PID=$!

cleanup() {
  kill "$ENGINE_PID" 2>/dev/null || true
  wait "$ENGINE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

ready=0
for _ in $(seq 1 180); do
  if curl -fsS "http://127.0.0.1:$ENGINE_PORT/healthz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo "CosyVoice failed to become ready; see $LOG_DIR/cosyvoice-server.log" >&2
  tail -80 "$LOG_DIR/cosyvoice-server.log" >&2 || true
  exit 1
fi

echo "TTS model: ready"
echo "Acceleration: $BACKEND"
echo "API: http://$HOST:$API_PORT/speak"
echo "Audio output: macOS default audio device (afplay)"
echo "Status: ready"

python3 "$ROOT/server/tts_gateway.py" \
  --host "$HOST" \
  --port "$API_PORT" \
  --engine-url "http://127.0.0.1:$ENGINE_PORT" \
  --audio-dir "$ROOT/runtime/audio"
