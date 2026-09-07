#!/usr/bin/env bash
set -euo pipefail

LABEL="com.ufun.tts"
ACCOUNT="$(id -un)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
KEYCHAIN_SERVICE="${TTS_KEYCHAIN_SERVICE:-com.ufun.tts.api-key}"

/bin/launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
/bin/rm -f "$PLIST"
/usr/bin/security delete-generic-password -a "$ACCOUNT" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 || true
echo "Uninstalled $LABEL and removed its Keychain API key."
