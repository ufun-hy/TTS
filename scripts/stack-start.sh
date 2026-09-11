#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=stack-common.sh
source "$ROOT/scripts/stack-common.sh"

TTS_HEALTH="http://127.0.0.1:8765/health"
CACHE_HEALTH="http://127.0.0.1:8000/health"
STUDIO_HEALTH="http://127.0.0.1:8770/api/health"
TRANSCRIPT_HEALTH="http://127.0.0.1:8771/api/health"
CACHE_PID="$STACK_PID_DIR/audio-cache.pid"
STUDIO_PID="$STACK_PID_DIR/text-studio.pid"
TRANSCRIPT_PID="$STACK_PID_DIR/recording-transcript.pid"

stack_load_tts_api_key

echo "Starting TTS stack..."

if stack_http_ok "$TTS_HEALTH"; then
  echo "TTS Gateway: already ready"
else
  if stack_port_has_unknown_listener 8765 "server/tts_gateway.py"; then
    echo "TTS Gateway: port 8765 is occupied by another process; refusing to replace it." >&2
    exit 1
  fi
  echo "TTS Gateway: starting"
  /bin/bash "$ROOT/scripts/service-start.sh"
  if ! stack_wait_http "$TTS_HEALTH" 180; then
    echo "TTS Gateway: failed to become ready. Check $STACK_LOG_DIR/service.error.log" >&2
    exit 1
  fi
  echo "TTS Gateway: ready"
fi

if stack_http_ok "$CACHE_HEALTH"; then
  stack_adopt_pid 8000 "scripts/audio-cache-server.py" "$CACHE_PID" || true
  echo "Audio Cache: already ready"
else
  if stack_port_has_unknown_listener 8000 "scripts/audio-cache-server.py"; then
    echo "Audio Cache: port 8000 is occupied by another process; refusing to replace it." >&2
    exit 1
  fi
  existing_cache_pid="$(stack_find_project_pid 8000 "scripts/audio-cache-server.py" || true)"
  if [[ -n "$existing_cache_pid" ]]; then
    printf '%s\n' "$existing_cache_pid" >"$CACHE_PID"
    stack_stop_project_process "Audio Cache" 8000 "scripts/audio-cache-server.py" "$CACHE_PID"
  fi
  echo "Audio Cache: starting"
  nohup /usr/bin/env \
    PYTHONPATH="$ROOT" \
    TTS_API_KEY="${TTS_API_KEY:-}" \
    AUDIO_CACHE_API_KEY="${AUDIO_CACHE_API_KEY:-}" \
    python3 "$ROOT/scripts/audio-cache-server.py" --host 0.0.0.0 --port 8000 \
    >"$STACK_LOG_DIR/audio-cache.log" 2>&1 </dev/null &
  printf '%s\n' "$!" >"$CACHE_PID"
  if ! stack_wait_http "$CACHE_HEALTH" 30; then
    echo "Audio Cache: failed to become ready. Check $STACK_LOG_DIR/audio-cache.log" >&2
    exit 1
  fi
  echo "Audio Cache: ready"
fi

# Migrate an older Text Studio process that was started directly with text_studio.py.
legacy_studio_pid="$(stack_find_project_pid 8770 "server/text_studio.py" || true)"
if [[ -n "$legacy_studio_pid" ]]; then
  printf '%s\n' "$legacy_studio_pid" >"$STUDIO_PID"
  echo "Text Studio: replacing legacy process"
  stack_stop_project_process "Legacy Text Studio" 8770 "server/text_studio.py" "$STUDIO_PID"
fi

if stack_http_ok "$STUDIO_HEALTH"; then
  stack_adopt_pid 8770 "server/text_studio_entry.py" "$STUDIO_PID" || true
  echo "Text Studio: already ready"
else
  if stack_port_has_unknown_listener 8770 "server/text_studio_entry.py"; then
    echo "Text Studio: port 8770 is occupied by another process; refusing to replace it." >&2
    exit 1
  fi
  existing_studio_pid="$(stack_find_project_pid 8770 "server/text_studio_entry.py" || true)"
  if [[ -n "$existing_studio_pid" ]]; then
    printf '%s\n' "$existing_studio_pid" >"$STUDIO_PID"
    stack_stop_project_process "Text Studio" 8770 "server/text_studio_entry.py" "$STUDIO_PID"
  fi
  echo "Text Studio: starting"
  nohup /usr/bin/env \
    PYTHONPATH="$ROOT" \
    TTS_API_KEY="${TTS_API_KEY:-}" \
    AUDIO_CACHE_API_KEY="${AUDIO_CACHE_API_KEY:-}" \
    TTS_GATEWAY_URL="http://127.0.0.1:8765" \
    AUDIO_CACHE_URL="http://127.0.0.1:8000" \
    /bin/bash "$ROOT/scripts/text-studio-start.sh" \
    >"$STACK_LOG_DIR/text-studio.log" 2>&1 </dev/null &
  printf '%s\n' "$!" >"$STUDIO_PID"
  if ! stack_wait_http "$STUDIO_HEALTH" 30; then
    echo "Text Studio: failed to become ready. Check $STACK_LOG_DIR/text-studio.log" >&2
    exit 1
  fi
  echo "Text Studio: ready"
fi

if stack_http_ok "$TRANSCRIPT_HEALTH"; then
  stack_adopt_pid 8771 "server/recording_transcript.py" "$TRANSCRIPT_PID" || true
  echo "Recording Transcript: already ready"
else
  if stack_port_has_unknown_listener 8771 "server/recording_transcript.py"; then
    echo "Recording Transcript: port 8771 is occupied by another process; leaving the rest of the stack running." >&2
  else
    existing_transcript_pid="$(stack_find_project_pid 8771 "server/recording_transcript.py" || true)"
    if [[ -n "$existing_transcript_pid" ]]; then
      printf '%s\n' "$existing_transcript_pid" >"$TRANSCRIPT_PID"
      stack_stop_project_process "Recording Transcript" 8771 "server/recording_transcript.py" "$TRANSCRIPT_PID"
    fi
    echo "Recording Transcript: starting"
    nohup /usr/bin/env \
      PYTHONPATH="$ROOT" \
      /bin/bash "$ROOT/scripts/recording-transcript-start.sh" \
      >"$STACK_LOG_DIR/recording-transcript.log" 2>&1 </dev/null &
    printf '%s\n' "$!" >"$TRANSCRIPT_PID"
    if stack_wait_http "$TRANSCRIPT_HEALTH" 30; then
      echo "Recording Transcript: ready"
    else
      echo "Recording Transcript: failed to become ready." >&2
      echo "--- recording-transcript.log ---" >&2
      /usr/bin/tail -n 80 "$STACK_LOG_DIR/recording-transcript.log" >&2 2>/dev/null || true
      echo "--- end recording-transcript.log ---" >&2
    fi
  fi
fi

echo
/bin/bash "$ROOT/scripts/stack-status.sh" || true
