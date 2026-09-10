#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
/bin/bash "$ROOT/scripts/stack-stop.sh"
/bin/bash "$ROOT/scripts/stack-start.sh"
