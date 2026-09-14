#!/usr/bin/env python3
"""Compare zero-shot synthesis while changing only the reference-audio start boundary."""

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

TRIMS_MS = (0, 30, 60, 100)


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
    if not ffmpeg:
        raise SystemExit("ffmpeg is required for the reference-boundary experiment")

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
    output_dir = ROOT / "runtime/ab/results" / f"{stamp}-{args.voice_id}-reference-boundary"
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

    rows: list[tuple[int, Path, Path]] = []
    for trim_ms in TRIMS_MS:
        label = f"{trim_ms:03d}ms"
        trimmed_ref = output_dir / f"reference-{label}.wav"
        synthesized = output_dir / f"synth-{label}.wav"
        trim_seconds = trim_ms / 1000.0

        # Re-encode every variant, including 0 ms, through the same ffmpeg path.
        # This keeps codec/container differences out of the comparison.
        filter_expr = f"atrim=start={trim_seconds:.3f},asetpts=PTS-STARTPTS"
        _run([
            ffmpeg,
            "-v", "error",
            "-nostdin",
            "-y",
            "-i", str(reference_audio),
            "-af", filter_expr,
            "-c:a", "pcm_s16le",
            str(trimmed_ref),
        ], timeout=300)

        print(f"\n=== reference start trim {trim_ms} ms ===", flush=True)
        _run([
            str(binary),
            *common,
            "--speech-tokenizer", str(tokenizer),
            "--campplus", str(campplus),
            "--prompt-audio", str(trimmed_ref),
            "--prompt-text", prompt_text,
            "--output", str(synthesized),
        ], env=env)

        if not synthesized.is_file() or not synthesized.stat().st_size:
            raise RuntimeError(f"no synthesis output for trim={trim_ms} ms")
        rows.append((trim_ms, trimmed_ref, synthesized))

    print("\nReference-boundary experiment complete.")
    print(f"Voice:      {args.voice_id}")
    print(f"Text:       {args.text}")
    print(f"Reference:  {reference_audio}")
    print(f"Output dir: {output_dir}")
    print("\nSTEP 1 — listen to the reference variants first:")
    for trim_ms, trimmed_ref, _ in rows:
        print(f'  {trim_ms:3d} ms: afplay "{trimmed_ref}"')
    print("\nOnly a trim whose reference still begins with the same complete spoken content is a valid comparison.")
    print("If 30/60/100 ms removes part of the first real phoneme, ignore that variant because reference audio and reference text no longer match.")
    print("\nSTEP 2 — listen to synthesis for the valid variants:")
    for trim_ms, _, synthesized in rows:
        print(f'  {trim_ms:3d} ms: afplay "{synthesized}"')
    print("\nInterpretation:")
    print("  artifact changes/disappears as a valid reference boundary changes -> reference conditioning is involved")
    print("  all valid variants sound the same -> model/HiFT/vocoder onset artifact is more likely")
    print("\nNo gateway, Audio Cache, Windows playback, or production prompt file was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
