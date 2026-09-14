#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VOICE_ID="${1:-speaker_a}"
TEXT="${2:-今天我们专门测试这一段语音开头是否干净。}"

CURRENT_BIN="$ROOT/runtime/bin/cosyvoice-cli"
MODEL="$ROOT/runtime/models/CosyVoice3-2512_Q8_0.gguf"
if [[ "$VOICE_ID" == "default" ]]; then
  PROMPT="$ROOT/runtime/models/prompt_speech.gguf"
else
  PROMPT="$ROOT/runtime/models/voices/$VOICE_ID.gguf"
fi

for required in "$CURRENT_BIN" "$MODEL" "$PROMPT"; do
  if [[ ! -s "$required" ]]; then
    echo "Missing runtime asset: $required" >&2
    exit 1
  fi
done

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "This A/B test targets Apple Silicon arm64." >&2
  exit 1
fi

AB_ROOT="$ROOT/runtime/ab/cosyvoice-v0.1.3"
PACKAGE="cosyvoice-1616b12-macos-arm64-miniaudio.tgz"
PACKAGE_URL="https://github.com/Lourdle/cosyvoice.cpp/releases/download/v0.1.3/$PACKAGE"
PACKAGE_SHA256="d5d09b22f10d2950049abb1391097fbc58b7e8c35d2645145c41bde5c1211bd4"
ARCHIVE="$AB_ROOT/$PACKAGE"
EXTRACT_DIR="$AB_ROOT/extracted"

mkdir -p "$AB_ROOT" "$EXTRACT_DIR"
if [[ ! -s "$ARCHIVE" ]]; then
  echo "Downloading CosyVoice v0.1.3 side-by-side runtime..."
  curl -fL --retry 3 "$PACKAGE_URL" -o "$ARCHIVE"
fi

actual_sha="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
if [[ "$actual_sha" != "$PACKAGE_SHA256" ]]; then
  echo "SHA-256 mismatch for $ARCHIVE" >&2
  echo "expected: $PACKAGE_SHA256" >&2
  echo "actual:   $actual_sha" >&2
  exit 1
fi

if ! find "$EXTRACT_DIR" -type f -name cosyvoice-cli -perm -111 -print -quit | grep -q .; then
  rm -rf "$EXTRACT_DIR"
  mkdir -p "$EXTRACT_DIR"
  tar -xzf "$ARCHIVE" -C "$EXTRACT_DIR"
fi

NEW_BIN="$(find "$EXTRACT_DIR" -type f -name cosyvoice-cli -perm -111 -print -quit)"
if [[ -z "$NEW_BIN" ]]; then
  echo "Could not find cosyvoice-cli in v0.1.3 package." >&2
  exit 1
fi
NEW_BIN_DIR="$(dirname "$NEW_BIN")"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="$ROOT/runtime/ab/results/$STAMP-$VOICE_ID"
mkdir -p "$OUT_DIR"

COMMON_ARGS=(
  --model "$MODEL"
  --prompt-speech "$PROMPT"
  --text "$TEXT"
  --mode zero-shot
  --backend auto
  --speed 1
  --seed 42
  --seed-policy fixed
  --threads 4
  --max-llm-len 4096
  --llm-kv-cache-type f16
  --llm-flash-attn 0
  --flow-flash-attn 0
)

run_case() {
  local label="$1" binary="$2" output="$3" libdir="$4"
  shift 4
  echo
  echo "=== $label ==="
  DYLD_LIBRARY_PATH="/opt/homebrew/opt/icu4c/lib:$libdir${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}" \
    "$binary" "${COMMON_ARGS[@]}" "$@" --output "$output"
  if [[ ! -s "$output" ]]; then
    echo "$label produced no WAV: $output" >&2
    exit 1
  fi
  if command -v ffprobe >/dev/null 2>&1; then
    ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$output" | awk -v label="$label" '{printf "%s duration: %.3fs\n", label, $1}'
  fi
}

run_case "A current-v0.1.1" "$CURRENT_BIN" "$OUT_DIR/A-current-v0.1.1.wav" "$ROOT/runtime/bin"
run_case "B v0.1.3-default-fade" "$NEW_BIN" "$OUT_DIR/B-v0.1.3-default-fade.wav" "$NEW_BIN_DIR"
run_case "C v0.1.3-no-fade" "$NEW_BIN" "$OUT_DIR/C-v0.1.3-no-fade.wav" "$NEW_BIN_DIR" --disable-fade-in

cat <<EOF

A/B generation complete.
Voice: $VOICE_ID
Text:  $TEXT

Listen in this order:
  afplay "$OUT_DIR/A-current-v0.1.1.wav"
  afplay "$OUT_DIR/B-v0.1.3-default-fade.wav"
  afplay "$OUT_DIR/C-v0.1.3-no-fade.wav"

Interpretation:
  A has onset artifact, B clean  -> runtime upgrade is the likely fix.
  A/B artifact, C clean          -> built-in fade-in interaction is implicated.
  A/B/C all artifact             -> likely model/HiFT/zero-shot generation, not playback/runtime fade.

Production runtime/bin was not modified.
EOF
