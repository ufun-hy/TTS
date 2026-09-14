#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
export PYTHONUNBUFFERED=1
SERVICE="${1:?missing service name}"
PYTHON="${STACK_PYTHON:?missing absolute Python path}"
# launchd's throttle is measured between starts, not from the last exit.
# This delay also guarantees a 10-second recovery interval after a long run.
printf '%s %s: launch requested; waiting 10s before starting\n' "$(date -Iseconds)" "$SERVICE"
sleep 10
# Never inherit stale credentials from launchd's global environment.
unset TTS_API_KEY AUDIO_CACHE_API_KEY
if [[ "$SERVICE" != "recording-transcript" ]]; then
  TTS_API_KEY="$(/usr/bin/security find-generic-password -a "$(id -un)" -s "${TTS_KEYCHAIN_SERVICE:-com.ufun.tts.api-key}" -w 2>/dev/null || true)"
  if [[ -z "$TTS_API_KEY" ]]; then
    echo "Existing TTS Keychain credential unavailable; refusing unauthenticated startup." >&2
    exit 1
  fi
  export TTS_API_KEY
fi
printf '%s %s: starting PID=%s\n' "$(date -Iseconds)" "$SERVICE" "$$"
case "$SERVICE" in
  audio-cache)
    exec "$PYTHON" "$ROOT/scripts/audio-cache-server.py" --host 0.0.0.0 --port 8000 ;;
  text-studio)
    export TEXT_STUDIO_PYTHON="$PYTHON"
    export TEXT_STUDIO_HOST=127.0.0.1 TEXT_STUDIO_PORT=8770
    export TTS_GATEWAY_URL=http://127.0.0.1:8765 AUDIO_CACHE_URL=http://127.0.0.1:8000
    exec /bin/bash "$ROOT/scripts/text-studio-start.sh" ;;
  recording-transcript)
    export RECORDING_TRANSCRIPT_HOST=127.0.0.1 RECORDING_TRANSCRIPT_PORT=8771
    exec /bin/bash "$ROOT/scripts/recording-transcript-start.sh" ;;
  *) echo "Unknown managed service: $SERVICE" >&2; exit 2 ;;
esac
