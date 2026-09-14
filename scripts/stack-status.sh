#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=stack-common.sh
source "$ROOT/scripts/stack-common.sh"

TTS_HEALTH="http://127.0.0.1:8765/health"
CACHE_HEALTH="http://127.0.0.1:8000/health"
all_ready=1
tts_body=""
cache_body=""

if tts_body="$(/usr/bin/curl -fsS --max-time 2 "$TTS_HEALTH" 2>/dev/null)"; then
  echo "TTS Gateway   READY    http://127.0.0.1:8765"
  engine_state="$(python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("tts", "unknown")).upper())' <<<"$tts_body" 2>/dev/null || echo UNKNOWN)"
  echo "CosyVoice     $engine_state"
else
  echo "TTS Gateway   DOWN     http://127.0.0.1:8765"
  echo "CosyVoice     UNKNOWN"
  all_ready=0
fi

python3 "$ROOT/scripts/stack-services.py" status || all_ready=0
cache_body="$(/usr/bin/curl -fsS --max-time 2 "$CACHE_HEALTH" 2>/dev/null || true)"

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
