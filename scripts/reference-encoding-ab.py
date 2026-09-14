#!/usr/bin/env python3
"""Compare zero-shot synthesis while changing only the reference WAV encoding."""

from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_datasets.prompt import build_zero_shot_prompt_text

ENCODINGS = (
    ("original", None),
    ("pcm16", "pcm_s16le"),
    ("pcm24", "pcm_s24le"),
    ("pcm32", "pcm_s32le"),
    ("float32", "pcm_f32le"),
)


def _load_rebuild_module():
    path = ROOT / "scripts/voice-prompts-rebuild.py"
    spec = importlib.util.spec_from_file_location("voice_prompts_rebuild", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(command: list[str], env: dict[str, str] | None = None, timeout: int = 1800) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=timeout)


def _probe(ffprobe: str, path: Path) -> str:
    completed = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_fmt,sample_rate,channels,bits_per_sample",
            "-of", "default=nw=1",
            str(path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return " ".join(line.strip() for line in completed.stdout.splitlines() if line.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("voice_id", nargs="?", default="speaker_a")
    parser.add_argument(
        "text",
        nargs="?",
        default="好，今天我们继续测试这一段语音开头是否完整。",
    )
    args = parser.parse_args()

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("ffmpeg and ffprobe are required for the reference-encoding experiment")

    rebuild = _load_rebuild_module()
    reference_audio, reference_text = rebuild._resolve_reference(args.voice_id)
    prompt_text = build_zero_shot_prompt_text(reference_text)

    binary = ROOT / "runtime/bin/cosyvoice-cli"
    model = ROOT / "runtime/models/CosyVoice3-2512_Q8_0.gguf"
    tokenizer = ROOT / "runtime/models/speech_tokenizer_v3.int8.onnx"
    campplus = ROOT / "runtime/models/campplus.int8.onnx"
    for required in (binary, model, tokenizer, campplus, reference_audio):
        if not required.is_file():
            raise SystemExit(f"missing runtime asset: {required}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = ROOT / "runtime/ab/results" / f"{stamp}-{args.voice_id}-reference-encoding"
    output_dir.mkdir(parents=True, exist_ok=False)

    env = dict(os.environ)
    env["DYLD_LIBRARY_PATH"] = f"/opt/homebrew/opt/icu4c/lib:{ROOT / 'runtime/bin'}" + (
        ":" + env["DYLD_LIBRARY_PATH"] if env.get("DYLD_LIBRARY_PATH") else ""
    )

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

    rows: list[tuple[str, Path, Path, str]] = []
    for label, codec in ENCODINGS:
        if codec is None:
            ref = reference_audio
        else:
            ref = output_dir / f"reference-{label}.wav"
            _run([
                ffmpeg,
                "-v", "error",
                "-nostdin",
                "-y",
                "-i", str(reference_audio),
                "-map_metadata", "-1",
                "-c:a", codec,
                str(ref),
            ], timeout=300)

        synthesized = output_dir / f"synth-{label}.wav"
        print(f"\n=== reference encoding: {label} ===", flush=True)
        print(f"reference: {ref}")
        print(f"format:    {_probe(ffprobe, ref)}")

        _run([
            str(binary),
            *common,
            "--speech-tokenizer", str(tokenizer),
            "--campplus", str(campplus),
            "--prompt-audio", str(ref),
            "--prompt-text", prompt_text,
            "--output", str(synthesized),
        ], env=env)

        if not synthesized.is_file() or not synthesized.stat().st_size:
            raise RuntimeError(f"no synthesis output for encoding={label}")
        rows.append((label, ref, synthesized, _probe(ffprobe, ref)))

    print("\nReference-encoding experiment complete.")
    print(f"Voice:      {args.voice_id}")
    print(f"Text:       {args.text}")
    print(f"Original:   {reference_audio}")
    print(f"Output dir: {output_dir}")
    print("\nReference formats:")
    for label, ref, _, description in rows:
        print(f"  {label:8s}: {description} | {ref}")

    print("\nListen to synthesis in this order:")
    for label, _, synthesized, _ in rows:
        print(f'  {label:8s}: afplay "{synthesized}"')

    print("\nReport two things for each file:")
    print("  onset artifact (e): yes/no")
    print("  continuous electrical noise: none/light/obvious")
    print("\nBest outcome: no onset artifact + no obvious continuous noise.")
    print("No reference trimming, gateway, Audio Cache, Windows playback, or production prompt file was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
