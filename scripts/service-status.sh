#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.ufun.tts"
TARGET="gui/$(id -u)/$LABEL"

if /bin/launchctl print "$TARGET" >/dev/null 2>&1; then
  echo "launchd: loaded"
else
  echo "launchd: not loaded"
fi

if health="$(/usr/bin/curl -fsS --max-time 3 http://127.0.0.1:8765/health 2>/dev/null)"; then
  echo "Gateway: running"
  echo "CosyVoice: $(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tts"])' <<<"$health")"
  echo "API: $(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"])' <<<"$health")"
else
  echo "Gateway: stopped or not ready"
  echo "CosyVoice: unknown"
  echo "API: not ready"
fi

echo "Port: 8765"
/usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN 2>/dev/null || true
echo "Logs: $ROOT/runtime/logs"
echo "--- Funnel ---"
"$ROOT/scripts/funnel-status.sh"
