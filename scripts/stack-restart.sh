#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/scripts/stack-services.py" preflight
/bin/bash "$ROOT/scripts/stack-stop.sh"
/bin/bash "$ROOT/scripts/stack-start.sh"
