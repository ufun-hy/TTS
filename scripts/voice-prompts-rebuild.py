#!/usr/bin/env python3
"""Rebuild configured CosyVoice3 prompt_speech files with a safe prompt boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_datasets.prompt import build_zero_shot_prompt_text

DEFAULT_TRANSCRIPT = "希望你以后能够做的比我还好呦。"
SPEAKER_DATASET = re.compile(r"^speaker_([abc])$")


def _resolve_reference(voice_id: str) -> tuple[Path, str]:
    if voice_id == "default":
        audio = ROOT / "runtime/models/zero_shot_prompt.wav"
        return audio, DEFAULT_TRANSCRIPT

    voice_dir = ROOT / "voices" / voice_id
    audio = voice_dir / "reference.wav"
    text = voice_dir / "reference.txt"
    if audio.is_file() and text.is_file():
        return audio, text.read_text(encoding="utf-8")

    match = SPEAKER_DATASET.fullmatch(voice_id)
    if match:
        reference_path = (
            ROOT / "runtime/voice-datasets" / f"speaker-{match.group(1)}" / "references/review-001.json"
        )
        if reference_path.is_file():
            reference = json.loads(reference_path.read_text(encoding="utf-8"))
            audio_path = Path(reference.get("audio_path", ""))
            if not audio_path.is_absolute():
                audio_path = ROOT / audio_path
            text_value = reference.get("text")
            if reference.get("quality_status") == "accepted" and audio_path.is_file() and isinstance(text_value, str) and text_value.strip():
                return audio_path, text_value

    raise FileNotFoundError(
        f"no rebuild reference for {voice_id}: expected voices/{voice_id}/reference.wav + reference.txt"
        + (" or reviewed runtime voice-dataset reference" if match else "")
    )


def _rebuild(voice_id: str, output: Path, binary: Path, tokenizer: Path, campplus: Path, env: dict[str, str]) -> None:
    audio, transcript = _resolve_reference(voice_id)
    prompt_text = build_zero_shot_prompt_text(transcript)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                str(binary),
                "--frontend-only",
                "--speech-tokenizer", str(tokenizer),
                "--campplus", str(campplus),
                "--prompt-audio", str(audio),
                "--prompt-text", prompt_text,
                "--prompt-speech-output", str(temporary),
            ],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=300,
        )
        if not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError(f"frontend returned no prompt for {voice_id}")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("voice_ids", nargs="*", help="voice IDs to rebuild; default: all configured voices")
    args = parser.parse_args()

    config_path = ROOT / "voices.json"
    voices = json.loads(config_path.read_text(encoding="utf-8"))
    selected = args.voice_ids or list(voices)
    unknown = [voice_id for voice_id in selected if voice_id not in voices]
    if unknown:
        parser.error("unknown voice IDs: " + ", ".join(unknown))

    binary = ROOT / "runtime/bin/cosyvoice-cli"
    tokenizer = ROOT / "runtime/models/speech_tokenizer_v3.int8.onnx"
    campplus = ROOT / "runtime/models/campplus.int8.onnx"
    for required in (binary, tokenizer, campplus):
        if not required.is_file():
            raise SystemExit(f"missing runtime asset: {required}")

    env = dict(os.environ)
    env["DYLD_LIBRARY_PATH"] = f"/opt/homebrew/opt/icu4c/lib:{ROOT / 'runtime/bin'}" + (
        ":" + env["DYLD_LIBRARY_PATH"] if env.get("DYLD_LIBRARY_PATH") else ""
    )

    rebuilt: list[str] = []
    skipped: list[tuple[str, str]] = []
    for voice_id in selected:
        entry = voices[voice_id]
        prompt_path = Path(entry["prompt_speech"])
        if not prompt_path.is_absolute():
            prompt_path = ROOT / prompt_path
        try:
            _rebuild(voice_id, prompt_path, binary, tokenizer, campplus, env)
        except FileNotFoundError as exc:
            skipped.append((voice_id, str(exc)))
            print(f"SKIP {voice_id}: {exc}")
            continue
        rebuilt.append(voice_id)
        print(f"REBUILT {voice_id}: {prompt_path}")

    print(f"rebuild complete: rebuilt={len(rebuilt)} skipped={len(skipped)}")
    if rebuilt:
        print("Restart the TTS stack so CosyVoice reloads the rebuilt speaker prompts:")
        print("  ./scripts/stack-restart.sh")
    return 0 if rebuilt else 1


if __name__ == "__main__":
    raise SystemExit(main())
