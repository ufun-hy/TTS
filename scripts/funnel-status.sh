#!/usr/bin/env bash
set -euo pipefail

if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE=(tailscale)
elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
  TAILSCALE=(/Applications/Tailscale.app/Contents/MacOS/Tailscale)
else
  echo "Tailscale: not installed"
  echo "Funnel: unknown"
  echo "Public URL: unknown"
  exit 0
fi

if TAILSCALE_BE_CLI=1 "${TAILSCALE[@]}" status >/dev/null 2>&1; then
  echo "Tailscale: connected"
else
  echo "Tailscale: not connected"
fi

funnel_status="$(TAILSCALE_BE_CLI=1 "${TAILSCALE[@]}" funnel status 2>&1 || true)"
if [[ -n "$funnel_status" ]]; then
  echo "$funnel_status"
  public_url="$(printf '%s\n' "$funnel_status" | grep -Eo 'https://[^[:space:]]+\.ts\.net' | head -1 || true)"
  echo "Public URL: ${public_url:-unknown}"
else
  echo "Funnel: disabled or unknown"
  echo "Public URL: unknown"
fi
