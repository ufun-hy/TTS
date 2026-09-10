#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=stack-common.sh
source "$ROOT/scripts/stack-common.sh"

TTS_HEALTH="http://127.0.0.1:8765/health"
CACHE_HEALTH="http://127.0.0.1:8000/health"
STUDIO_HEALTH="http://127.0.0.1:8770/api/health"
all_ready=1
cache_body=""

if stack_http_ok "$TTS_HEALTH"; then
  echo "TTS Gateway   READY    http://127.0.0.1:8765"
else
  echo "TTS Gateway   DOWN     http://127.0.0.1:8765"
  all_ready=0
fi

if cache_body="$(/usr/bin/curl -fsS --max-time 2 "$CACHE_HEALTH" 2>/dev/null)"; then
  echo "Audio Cache   READY    http://127.0.0.1:8000"
else
  echo "Audio Cache   DOWN     http://127.0.0.1:8000"
  all_ready=0
fi

if stack_http_ok "$STUDIO_HEALTH"; then
  echo "Text Studio   READY    http://127.0.0.1:8770"
else
  echo "Text Studio   DOWN     http://127.0.0.1:8770"
  all_ready=0
fi

if [[ -n "$cache_body" ]]; then
  windows_state="$(python3 -c 'import json,sys; print("CONNECTED" if json.load(sys.stdin).get("client_connected") else "OFFLINE")' <<<"$cache_body" 2>/dev/null || echo UNKNOWN)"
else
  windows_state="UNKNOWN"
fi
echo "Windows       $windows_state"

lan_ip="$(stack_lan_ip || true)"
if [[ -n "$lan_ip" ]]; then
  echo "Windows URL   http://$lan_ip:8000"
else
  echo "Windows URL   unavailable (no LAN IPv4 detected)"
fi

echo "Logs          $STACK_LOG_DIR"

if [[ "$all_ready" -eq 1 ]]; then
  echo
  echo "TTS Stack: READY"
  exit 0
fi

echo
echo "TTS Stack: INCOMPLETE"
exit 1
