#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.ufun.tts"
UID_VALUE="$(id -u)"
ACCOUNT="$(id -un)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$ROOT/runtime/logs"
KEYCHAIN_SERVICE="${TTS_KEYCHAIN_SERVICE:-com.ufun.tts.api-key}"

mkdir -p "$PLIST_DIR" "$LOG_DIR"
if ! /usr/bin/security find-generic-password -a "$ACCOUNT" -s "$KEYCHAIN_SERVICE" -w >/dev/null 2>&1; then
  key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  /usr/bin/security add-generic-password -a "$ACCOUNT" -s "$KEYCHAIN_SERVICE" -w "$key" -U >/dev/null
  echo "Created API key in macOS Keychain service: $KEYCHAIN_SERVICE"
else
  echo "Using existing API key in macOS Keychain service: $KEYCHAIN_SERVICE"
fi

python3 - "$PLIST" "$ROOT" "$LOG_DIR" <<'PY'
import plistlib
import sys

plist_path, root, log_dir = sys.argv[1:]
payload = {
    "Label": "com.ufun.tts",
    "ProgramArguments": ["/bin/bash", f"{root}/start.sh"],
    "WorkingDirectory": root,
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 10,
    "ProcessType": "Interactive",
    "StandardOutPath": f"{log_dir}/service.log",
    "StandardErrorPath": f"{log_dir}/service.error.log",
}
with open(plist_path, "wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY

/bin/chmod 600 "$PLIST"
/bin/launchctl bootout "gui/$UID_VALUE/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID_VALUE" "$PLIST"
/bin/launchctl enable "gui/$UID_VALUE/$LABEL" || true
/bin/launchctl kickstart -k "gui/$UID_VALUE/$LABEL"
echo "Installed and started $LABEL"
