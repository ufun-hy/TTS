#!/usr/bin/env bash
set -euo pipefail

if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE=(tailscale)
elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
  TAILSCALE=(/Applications/Tailscale.app/Contents/MacOS/Tailscale)
else
  echo "Tailscale is not installed."
  exit 0
fi

TAILSCALE_BE_CLI=1 "${TAILSCALE[@]}" funnel --https=443 8765 off
echo "Funnel disabled."
