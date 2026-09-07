#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VOICE_ID="${1:-}"
if [[ -z "$VOICE_ID" || ! "$VOICE_ID" =~ ^[A-Za-z0-9_-]+$ || "$VOICE_ID" == "default" ]]; then
  echo "usage: $0 <voice_id>" >&2
  echo "voice_id must be letters, numbers, _ or -, and cannot be default" >&2
  exit 2
fi

VOICE_DIR="$ROOT/voices/$VOICE_ID"
REF_AUDIO="$VOICE_DIR/reference.wav"
REF_TEXT="$VOICE_DIR/reference.txt"
OUT_DIR="$ROOT/runtime/models/voices"
OUT_FILE="$OUT_DIR/$VOICE_ID.gguf"
if [[ ! -s "$REF_AUDIO" || ! -s "$REF_TEXT" ]]; then
  echo "Missing $REF_AUDIO or $REF_TEXT" >&2
  exit 1
fi
if [[ ! -x "$ROOT/runtime/bin/cosyvoice-cli" ]]; then
  echo "Missing cosyvoice-cli; run ./scripts/install.sh first" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
export DYLD_LIBRARY_PATH="/opt/homebrew/opt/icu4c/lib:$ROOT/runtime/bin${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
"$ROOT/runtime/bin/cosyvoice-cli" \
  --frontend-only \
  --speech-tokenizer "$ROOT/runtime/models/speech_tokenizer_v3.int8.onnx" \
  --campplus "$ROOT/runtime/models/campplus.int8.onnx" \
  --prompt-audio "$REF_AUDIO" \
  --prompt-text "$(< "$REF_TEXT")" \
  --prompt-speech-output "$OUT_FILE"

python3 - "$ROOT/voices.json" "$VOICE_ID" "runtime/models/voices/$VOICE_ID.gguf" <<'PY'
import json
import sys

path, voice_id, prompt_speech = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    voices = json.load(handle)
voices[voice_id] = {"prompt_speech": prompt_speech}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(voices, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

echo "Prepared voice: $VOICE_ID"
