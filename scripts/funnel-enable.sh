#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE=(tailscale)
elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
  TAILSCALE=(/Applications/Tailscale.app/Contents/MacOS/Tailscale)
else
  echo "Tailscale is not installed. Install the official macOS client first." >&2
  exit 1
fi

if ! curl -fsS --max-time 3 http://127.0.0.1:8765/health >/dev/null; then
  echo "TTS Gateway is not healthy; refusing to enable Funnel." >&2
  exit 1
fi
if ! TAILSCALE_BE_CLI=1 "${TAILSCALE[@]}" status >/dev/null 2>&1; then
  echo "Tailscale is not connected. Complete Tailscale login first." >&2
  exit 1
fi

TAILSCALE_BE_CLI=1 "${TAILSCALE[@]}" funnel --bg 8765
echo "Funnel enabled. Current status:"
TAILSCALE_BE_CLI=1 "${TAILSCALE[@]}" funnel status
