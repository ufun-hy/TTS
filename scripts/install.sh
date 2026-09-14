#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$ROOT/runtime/bin"
MODEL_DIR="$ROOT/runtime/models"
ENGINE_VERSION="v0.1.3"
ENGINE_ARCHIVE="cosyvoice-1616b12-macos-arm64-miniaudio.tgz"
HF_ROOT="https://huggingface.co/Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF/resolve/main"
PROMPT_CONTRACT_VERSION="cosyvoice3-eop-v1"
PROMPT_CONTRACT_FILE="$MODEL_DIR/prompt_speech.contract"

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "This setup targets Apple Silicon (arm64)." >&2
  exit 1
fi

mkdir -p "$BIN_DIR" "$MODEL_DIR" "$ROOT/runtime/audio" "$ROOT/runtime/logs"

download() {
  local url="$1" dest="$2"
  if [[ -s "$dest" ]]; then
    echo "exists: $dest"
    return
  fi
  echo "download: $url"
  curl -fL --retry 3 --continue-at - "$url" -o "$dest"
}

if ! brew list icu4c >/dev/null 2>&1; then
  brew install icu4c
fi

if [[ ! -x "$BIN_DIR/cosyvoice-server" || ! -x "$BIN_DIR/cosyvoice-cli" || ! -s "$BIN_DIR/libcosyvoice.0.1.3.dylib" ]]; then
  archive="$ROOT/runtime/$ENGINE_ARCHIVE"
  download "https://github.com/Lourdle/cosyvoice.cpp/releases/download/$ENGINE_VERSION/$ENGINE_ARCHIVE" "$archive"
  tar -xzf "$archive" -C "$BIN_DIR"
  rm -f "$archive"
fi

download "$HF_ROOT/CosyVoice3-2512_Q8_0.gguf?download=true" "$MODEL_DIR/CosyVoice3-2512_Q8_0.gguf"
download "$HF_ROOT/frontend-onnx/speech_tokenizer_v3.int8.onnx?download=true" "$MODEL_DIR/speech_tokenizer_v3.int8.onnx"
download "$HF_ROOT/frontend-onnx/campplus.int8.onnx?download=true" "$MODEL_DIR/campplus.int8.onnx"
download "https://raw.githubusercontent.com/QwenAudio/CosyVoice/main/asset/zero_shot_prompt.wav" "$MODEL_DIR/zero_shot_prompt.wav"

export DYLD_LIBRARY_PATH="/opt/homebrew/opt/icu4c/lib:$BIN_DIR${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
current_contract="$(cat "$PROMPT_CONTRACT_FILE" 2>/dev/null || true)"
if [[ ! -s "$MODEL_DIR/prompt_speech.gguf" || "$current_contract" != "$PROMPT_CONTRACT_VERSION" ]]; then
  echo "extracting prompt_speech.gguf with CosyVoice3 prompt boundary"
  PROMPT_TEXT="$(PYTHONPATH="$ROOT" python3 - <<'PY'
from voice_datasets.prompt import build_zero_shot_prompt_text
print(build_zero_shot_prompt_text("希望你以后能够做的比我还好呦。"), end="")
PY
)"
  "$BIN_DIR/cosyvoice-cli" \
    --frontend-only \
    --speech-tokenizer "$MODEL_DIR/speech_tokenizer_v3.int8.onnx" \
    --campplus "$MODEL_DIR/campplus.int8.onnx" \
    --prompt-audio "$MODEL_DIR/zero_shot_prompt.wav" \
    --prompt-text "$PROMPT_TEXT" \
    --prompt-speech-output "$MODEL_DIR/prompt_speech.gguf"
  printf '%s\n' "$PROMPT_CONTRACT_VERSION" > "$PROMPT_CONTRACT_FILE"
fi

echo "Installed CosyVoice runtime and Fun-CosyVoice3 model assets."
