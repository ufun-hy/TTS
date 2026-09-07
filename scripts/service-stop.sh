#!/usr/bin/env bash
set -euo pipefail

LABEL="com.ufun.tts"
/bin/launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
echo "Stopped $LABEL"
