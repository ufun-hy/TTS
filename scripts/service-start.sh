#!/usr/bin/env bash
set -euo pipefail

LABEL="com.ufun.tts"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TARGET="gui/$(id -u)/$LABEL"
/bin/launchctl print "$TARGET" >/dev/null 2>&1 || /bin/launchctl bootstrap "gui/$(id -u)" "$PLIST"
/bin/launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "Started $LABEL"
