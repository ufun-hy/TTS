"""Run the Windows target-machine ASR/TTS smoke benchmark.

This script intentionally does not download weights.  Pass external model
paths and keep the JSON result outside the repository when benchmarking.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import wave
from urllib import request
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from recording_transcript.qwen_asr_windows import transcribe


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / max(1, handle.getframerate())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True, help="decoded mono PCM WAV")
    parser.add_argument("--asr-model", type=Path, required=True)
    parser.add_argument("--tts-url", default="", help="CosyVoice /tts endpoint, e.g. http://127.0.0.1:8766")
    parser.add_argument("--text", default="", help="TTS text; defaults to the first ASR result")
    parser.add_argument("--voice", default="default")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source_seconds = wav_seconds(args.audio)
    started = time.perf_counter()
    asr = transcribe(args.audio, args.asr_model)
    asr_elapsed = time.perf_counter() - started
    result = {
        "audio": str(args.audio), "asr_model": str(args.asr_model), "source_seconds": source_seconds,
        "asr_seconds": round(asr_elapsed, 3), "asr_rtf": round(asr_elapsed / source_seconds, 4) if source_seconds else None,
        "segments": asr.get("segments", []), "text": asr.get("text", ""),
    }
    if args.tts_url:
        text = args.text.strip() or str(asr.get("text", "")).strip()
        payload = json.dumps({"text": text, "voice": args.voice, "response_format": "wav", "stream": False}, ensure_ascii=False).encode("utf-8")
        started = time.perf_counter()
        with request.urlopen(request.Request(args.tts_url.rstrip("/") + "/tts", data=payload, headers={"Content-Type": "application/json"}, method="POST"), timeout=900) as response:
            audio = response.read()
        tts_elapsed = time.perf_counter() - started
        result.update({"tts_seconds": round(tts_elapsed, 3), "tts_bytes": len(audio), "tts_rtf": None, "voice": args.voice})
        temporary = args.output.with_suffix(".tts.wav")
        temporary.write_bytes(audio)
        try:
            duration = wav_seconds(temporary)
            result["tts_audio_seconds"] = duration
            result["tts_rtf"] = round(tts_elapsed / duration, 4) if duration else None
        finally:
            temporary.unlink(missing_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
