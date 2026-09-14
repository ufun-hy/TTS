#!/usr/bin/env python3
"""Compare precomputed prompt_speech against direct reference zero-shot synthesis."""

from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_datasets.prompt import build_zero_shot_prompt_text


def _load_rebuild_module():
    path = ROOT / "scripts/voice-prompts-rebuild.py"
    spec = importlib.util.spec_from_file_location("voice_prompts_rebuild", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(label: str, command: list[str], env: dict[str, str], output: Path) -> None:
    print(f"\n=== {label} ===", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=1800)
    if not output.is_file() or not output.stat().st_size:
        raise RuntimeError(f"{label} produced no WAV: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("voice_id", nargs="?", default="speaker_a")
    parser.add_argument(
        "text",
        nargs="?",
        default="好，今天我们继续测试这一段语音开头是否完整。",
    )
    args = parser.parse_args()

    voices = json.loads((ROOT / "voices.json").read_text(encoding="utf-8"))
    if args.voice_id not in voices:
        parser.error(f"unknown voice: {args.voice_id}")

    rebuild = _load_rebuild_module()
    reference_audio, reference_text = rebuild._resolve_reference(args.voice_id)
    prompt_text = build_zero_shot_prompt_text(reference_text)

    prompt_path = Path(voices[args.voice_id]["prompt_speech"])
    if not prompt_path.is_absolute():
        prompt_path = ROOT / prompt_path

    binary = ROOT / "runtime/bin/cosyvoice-cli"
    model = ROOT / "runtime/models/CosyVoice3-2512_Q8_0.gguf"
    tokenizer = ROOT / "runtime/models/speech_tokenizer_v3.int8.onnx"
    campplus = ROOT / "runtime/models/campplus.int8.onnx"
    for required in (binary, model, tokenizer, campplus, prompt_path, reference_audio):
        if not required.is_file():
            raise SystemExit(f"missing runtime asset: {required}")

    output_dir = ROOT / "runtime/ab/results" / (
        datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{args.voice_id}-direct-reference"
    )
    output_dir.mkdir(parents=True)
    precomputed = output_dir / "A-precomputed-prompt.wav"
    direct = output_dir / "B-direct-reference.wav"

    common = [
        "--model", str(model),
        "--text", args.text,
        "--mode", "zero-shot",
        "--backend", "auto",
        "--speed", "1",
        "--seed", "42",
        "--seed-policy", "fixed",
        "--threads", "4",
        "--max-llm-len", "4096",
        "--llm-kv-cache-type", "f16",
        "--llm-flash-attn", "0",
        "--flow-flash-attn", "0",
    ]

    env = dict(os.environ)
    env["DYLD_LIBRARY_PATH"] = f"/opt/homebrew/opt/icu4c/lib:{ROOT / 'runtime/bin'}" + (
        ":" + env["DYLD_LIBRARY_PATH"] if env.get("DYLD_LIBRARY_PATH") else ""
    )

    _run(
        "A precomputed prompt_speech",
        [str(binary), *common, "--prompt-speech", str(prompt_path), "--output", str(precomputed)],
        env,
        precomputed,
    )
    _run(
        "B direct reference.wav + reference.txt",
        [
            str(binary),
            *common,
            "--speech-tokenizer", str(tokenizer),
            "--campplus", str(campplus),
            "--prompt-audio", str(reference_audio),
            "--prompt-text", prompt_text,
            "--output", str(direct),
        ],
        env,
        direct,
    )

    print("\nA/B generation complete.")
    print(f"Voice: {args.voice_id}")
    print(f"Text:  {args.text}")
    print(f"Reference: {reference_audio}")
    print("\nListen in this order:")
    print(f'  afplay "{precomputed}"')
    print(f'  afplay "{direct}"')
    print("\nInterpretation:")
    print("  A has artifact, B clean -> prompt_speech precompute/registration path is the likely cause.")
    print("  A and B both have it   -> artifact is deeper in zero-shot/model generation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
