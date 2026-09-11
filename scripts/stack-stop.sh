#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=stack-common.sh
source "$ROOT/scripts/stack-common.sh"

stack_stop_project_process "Text Studio" 8770 "server/text_studio_entry.py" "$STACK_PID_DIR/text-studio.pid"
stack_stop_project_process "Legacy Text Studio" 8770 "server/text_studio.py" "$STACK_PID_DIR/text-studio.pid"
stack_stop_project_process "Recording Transcript" 8771 "server/recording_transcript.py" "$STACK_PID_DIR/recording-transcript.pid"
stack_stop_project_process "Audio Cache" 8000 "scripts/audio-cache-server.py" "$STACK_PID_DIR/audio-cache.pid"
/bin/bash "$ROOT/scripts/service-stop.sh"
# launchctl bootout returns before the listener necessarily exits.
for ((attempt = 0; attempt < 40; attempt++)); do
  [[ -z "$(stack_find_project_pid 8765 "server/tts_gateway.py" || true)" ]] && break
  sleep 0.25
done
if [[ -n "$(stack_find_project_pid 8765 "server/tts_gateway.py" || true)" ]]; then
  echo "TTS Gateway: did not stop; refusing to restart over the old listener." >&2
  exit 1
fi

echo "TTS Gateway: stopped"
echo "TTS stack stopped."
