#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=stack-common.sh
source "$ROOT/scripts/stack-common.sh"

python3 "$ROOT/scripts/stack-services.py" stop
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
