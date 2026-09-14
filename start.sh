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
KEYCHAIN_SERVICE="${TTS_KEYCHAIN_SERVICE:-com.ufun.tts.api-key}"
ENGINE_IDLE_SECONDS="${TTS_ENGINE_IDLE_SECONDS:-600}"

if [[ "${1:-}" == "--lan" ]]; then
  HOST="0.0.0.0"
elif [[ "${1:-}" != "" ]]; then
  echo "usage: ./start.sh [--lan]" >&2
  exit 2
fi

required=(
  "$BIN_DIR/cosyvoice-server"
  "$MODEL_DIR/CosyVoice3-2512_Q8_0.gguf"
  "$ROOT/voices.json"
  "$MODEL_DIR/prompt_speech.gguf"
)
for path in "${required[@]}"; do
  if [[ ! -s "$path" ]]; then
    echo "Missing $path" >&2
    echo "Run: ./scripts/install.sh" >&2
    exit 1
  fi
done

if [[ -z "${TTS_API_KEY:-}" && -x /usr/bin/security ]]; then
  TTS_API_KEY="$(/usr/bin/security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
  export TTS_API_KEY
fi
if [[ -z "${TTS_API_KEY:-}" ]]; then
  echo "Missing TTS_API_KEY. Run ./scripts/service-install.sh or export TTS_API_KEY." >&2
  exit 1
fi

for port in "$ENGINE_PORT" "$API_PORT"; do
  if /usr/sbin/lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | grep -q .; then
    echo "TCP port $port is already in use; refusing to start a duplicate instance." >&2
    exit 1
  fi
done

mkdir -p "$LOG_DIR" "$ROOT/runtime/audio"
export DYLD_LIBRARY_PATH="/opt/homebrew/opt/icu4c/lib:$BIN_DIR${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

ENGINE_VERBOSE_ARGS=()
if [[ "${COSYVOICE_VERBOSE:-0}" == "1" ]]; then
  ENGINE_VERBOSE_ARGS+=(--engine-verbose)
fi

echo "TTS Gateway: starting"
echo "CosyVoice engine: on-demand, idle sleep after ${ENGINE_IDLE_SECONDS}s"
echo "Acceleration: $BACKEND"
echo "API: http://$HOST:$API_PORT/speak"
echo "Authentication: Bearer API key"

# Bash 3.2 with `set -u` errors when an empty array is expanded directly.
exec python3 "$ROOT/server/tts_gateway.py" \
  --host "$HOST" \
  --port "$API_PORT" \
  --engine-url "http://127.0.0.1:$ENGINE_PORT" \
  --engine-bin "$BIN_DIR/cosyvoice-server" \
  --engine-model "$MODEL_DIR/CosyVoice3-2512_Q8_0.gguf" \
  --engine-backend "$BACKEND" \
  --engine-log "$LOG_DIR/cosyvoice-server.log" \
  --engine-idle-seconds "$ENGINE_IDLE_SECONDS" \
  ${ENGINE_VERBOSE_ARGS[@]-} \
  --audio-dir "$ROOT/runtime/audio" \
  --voices-config "$ROOT/voices.json" \
  --rate-limit-per-minute "${TTS_RATE_LIMIT_PER_MINUTE:-30}"
