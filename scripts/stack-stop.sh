#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=stack-common.sh
source "$ROOT/scripts/stack-common.sh"

stack_stop_project_process "Text Studio" 8770 "server/text_studio.py" "$STACK_PID_DIR/text-studio.pid"
stack_stop_project_process "Audio Cache" 8000 "scripts/audio-cache-server.py" "$STACK_PID_DIR/audio-cache.pid"
/bin/bash "$ROOT/scripts/service-stop.sh"

echo "TTS Gateway: stopped"
echo "TTS stack stopped."
