#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$ROOT/runtime/bin"
MODEL_DIR="$ROOT/runtime/models"
ENGINE_VERSION="v0.1.1"
ENGINE_ARCHIVE="cosyvoice-3c7448c-macos-arm64-miniaudio.tgz"
HF_ROOT="https://huggingface.co/Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF/resolve/main"

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

if [[ ! -x "$BIN_DIR/cosyvoice-server" || ! -x "$BIN_DIR/cosyvoice-cli" ]]; then
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
if [[ ! -s "$MODEL_DIR/prompt_speech.gguf" ]]; then
  echo "extracting prompt_speech.gguf"
  "$BIN_DIR/cosyvoice-cli" \
    --frontend-only \
    --speech-tokenizer "$MODEL_DIR/speech_tokenizer_v3.int8.onnx" \
    --campplus "$MODEL_DIR/campplus.int8.onnx" \
    --prompt-audio "$MODEL_DIR/zero_shot_prompt.wav" \
    --prompt-text "希望你以后能够做的比我还好呦。" \
    --prompt-speech-output "$MODEL_DIR/prompt_speech.gguf"
fi

echo "Installed CosyVoice runtime and Fun-CosyVoice3 model assets."
