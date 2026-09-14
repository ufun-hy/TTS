#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=stack-common.sh
source "$ROOT/scripts/stack-common.sh"

TTS_HEALTH="http://127.0.0.1:8765/health"
# Fail before touching running services if credentials cannot survive detachment.
python3 "$ROOT/scripts/stack-services.py" preflight

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

result=0
python3 "$ROOT/scripts/stack-services.py" start || result=1
/bin/bash "$ROOT/scripts/stack-status.sh" || result=1
exit "$result"
