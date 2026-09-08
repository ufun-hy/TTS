#!/usr/bin/env python3
"""Run a local ASR backend and write timestamped ASR JSON."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Optional


SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4"}
WORD_SEGMENT_BOUNDARY_TOLERANCE = 0.1


class IngestError(ValueError):
    """A user-correctable ingest or ASR configuration error."""


def _number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"invalid {field}: {value!r}") from exc
    if number < 0:
        raise IngestError(f"invalid {field}: must be non-negative")
    return round(number, 6)


def _field(value: Any, name: str) -> Any:
    return getattr(value, name, value.get(name) if isinstance(value, dict) else None)


def _word_value(value: Any) -> str:
    word = _field(value, "word")
    if word is None:
        word = _field(value, "text")
    return str(word or "").strip()


def _normalize_word(value: Any, segment_id: str, index: int) -> Dict[str, Any]:
    word = _word_value(value)
    if not word:
        raise IngestError(f"{segment_id}: word {index} is empty")
    start = _number(_field(value, "start"), f"{segment_id} word {index} start")
    end = _number(_field(value, "end"), f"{segment_id} word {index} end")
    if end <= start:
        raise IngestError(f"{segment_id}: word {index} end must be greater than start")
    return {"word": word, "start": start, "end": end}


def _normalize_segments(raw_segments: Iterable[Any]) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_segments, 1):
        segment_id = str(_field(raw, "id") or f"asr_{index:04d}")
        raw_words = _field(raw, "words") or []
        words = [_normalize_word(word, segment_id, word_index) for word_index, word in enumerate(raw_words, 1)]
        if not words:
            raise IngestError(f"{segment_id}: backend returned no word timestamps")
        for previous, current in zip(words, words[1:]):
            if current["start"] < previous["end"]:
                raise IngestError(f"{segment_id}: overlapping word timestamps")

        raw_text = _field(raw, "text")
        text = str(raw_text if raw_text is not None else "").strip()
        if not text:
            text = "".join(word["word"] for word in words)
        start_value = _field(raw, "start")
        end_value = _field(raw, "end")
        start = _number(start_value if start_value is not None else words[0]["start"], f"{segment_id} start")
        end = _number(end_value if end_value is not None else words[-1]["end"], f"{segment_id} end")
        if end <= start:
            raise IngestError(f"{segment_id}: end must be greater than start")
        boundary_drift = max(start - words[0]["start"], words[-1]["end"] - end, 0.0)
        if boundary_drift > WORD_SEGMENT_BOUNDARY_TOLERANCE:
            raise IngestError(f"{segment_id}: word timestamps must fit inside segment timestamps")
        # ponytail: clamp <=100ms decoder boundary rounding; larger drift fails validation.
        words[0]["start"] = max(words[0]["start"], start)
        words[-1]["end"] = min(words[-1]["end"], end)
        if words[0]["start"] >= words[0]["end"] or words[-1]["start"] >= words[-1]["end"]:
            raise IngestError(f"{segment_id}: clamped word timestamp is empty")
        segments.append({"id": segment_id, "start": start, "end": end, "text": text, "words": words})
    if not segments:
        raise IngestError("ASR returned no speech segments")
    return segments


def normalize_result(result: Any, source: Path, duration: float, language: str) -> Dict[str, Any]:
    """Convert common local Whisper result shapes into the project schema."""
    if not isinstance(result, dict):
        raise IngestError("ASR backend returned an invalid result")
    raw_segments = result.get("segments")
    if raw_segments is None:
        raise IngestError("ASR backend returned no segments")
    segments = _normalize_segments(raw_segments)
    detected_language = str(result.get("language") or language)
    actual_duration = max(duration, max(item["end"] for item in segments))
    return {
        "schema_version": 1,
        "source": str(source.resolve()),
        "duration": round(actual_duration, 6),
        "language": detected_language,
        "segments": segments,
    }


def _materialize_faster_segments(segments: Iterable[Any]) -> Dict[str, Any]:
    values = []
    for segment in segments:
        words = []
        for word in getattr(segment, "words", None) or []:
            words.append({"word": word.word, "start": word.start, "end": word.end})
        values.append({"start": segment.start, "end": segment.end, "text": segment.text, "words": words})
    return {"segments": values}


def transcribe_mlx(audio: Path, model: Optional[str], language: str) -> Dict[str, Any]:
    import mlx_whisper  # type: ignore

    options: Dict[str, Any] = {"word_timestamps": True, "language": language, "task": "transcribe"}
    options["path_or_hf_repo"] = model
    return mlx_whisper.transcribe(str(audio), **options)


def transcribe_faster_whisper(audio: Path, model: Optional[str], language: str) -> Dict[str, Any]:
    from faster_whisper import WhisperModel  # type: ignore

    device = "auto"
    compute_type = "default"
    whisper_model = WhisperModel(model, device=device, compute_type=compute_type)
    segments, info = whisper_model.transcribe(str(audio), language=language, word_timestamps=True)
    result = _materialize_faster_segments(segments)
    result["language"] = getattr(info, "language", language)
    return result


def transcribe_openai_whisper(audio: Path, model: Optional[str], language: str) -> Dict[str, Any]:
    import whisper  # type: ignore

    whisper_model = whisper.load_model(model)
    return whisper_model.transcribe(str(audio), language=language, word_timestamps=True, task="transcribe")


def _whisper_cpp_binary() -> Optional[str]:
    return shutil.which("whisper-cli") or shutil.which("main")


def _parse_whisper_cpp_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        # whisper.cpp JSON offsets are milliseconds.
        return round(float(value) / 1000.0, 6)
    text = str(value or "").replace(",", ".")
    parts = text.split(":")
    if len(parts) != 3:
        raise IngestError(f"invalid whisper.cpp timestamp: {value!r}")
    hours, minutes, seconds = parts
    return round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 6)


def transcribe_whisper_cpp(audio: Path, model: Optional[str], language: str) -> Dict[str, Any]:
    binary = _whisper_cpp_binary()
    if not binary:
        raise IngestError("whisper.cpp CLI is not available")
    output_prefix = audio.parent / "whisper-output"
    completed = subprocess.run(
        [binary, "-m", model, "-f", str(audio), "-l", language, "-ojf", "-of", str(output_prefix), "-ml", "1", "-sow", "-np"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown whisper.cpp error"
        raise IngestError(message)
    json_path = output_prefix.with_suffix(".json")
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"whisper.cpp did not produce {json_path}") from exc
    segments = []
    for item in raw.get("transcription", []):
        text = str(item.get("text") or "").strip()
        timestamps = item.get("timestamps") or {}
        offsets = item.get("offsets") or {}
        start_value = timestamps.get("from", offsets.get("from"))
        end_value = timestamps.get("to", offsets.get("to"))
        if not text or start_value is None or end_value is None:
            continue
        start = _parse_whisper_cpp_time(start_value)
        end = _parse_whisper_cpp_time(end_value)
        if end <= start:
            continue
        # -ml 1 + -sow asks whisper.cpp for one word per transcription item.
        segments.append({"start": start, "end": end, "text": text, "words": [{"word": text, "start": start, "end": end}]})
    return {"segments": segments, "language": (raw.get("result") or {}).get("language", language)}


def available_backends() -> List[str]:
    detected = [
        name
        for name, module in (("mlx-whisper", "mlx_whisper"), ("faster-whisper", "faster_whisper"), ("openai-whisper", "whisper"))
        if importlib.util.find_spec(module) is not None
    ]
    if _whisper_cpp_binary():
        detected.append("whisper.cpp")
    return detected


def choose_backend(requested: str) -> str:
    if requested != "auto":
        if requested not in available_backends():
            installed = ", ".join(available_backends()) or "none"
            raise IngestError(f"requested backend {requested!r} is not available (detected: {installed})")
        return requested
    detected = available_backends()
    if detected:
        return detected[0]
    raise IngestError(
        "no local ASR backend found; install/use an existing mlx-whisper, faster-whisper, "
        "or openai-whisper environment and pass --model when needed"
    )


def _duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        return max(0.0, float(completed.stdout.strip()))
    except ValueError:
        return 0.0


def _audio_for_asr(source: Path, workdir: Path) -> Path:
    # Normalize every supported format so all local backends receive the same PCM input.
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise IngestError("ffmpeg is required to decode wav/mp3/m4a/mp4 input")
    output = workdir / "audio.wav"
    completed = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown ffmpeg error"
        raise IngestError(f"could not decode audio: {message}")
    return output


def ingest(source: Path, output: Path, backend: str = "auto", model: Optional[str] = None, language: str = "zh") -> Dict[str, Any]:
    source = source.expanduser()
    if not source.is_file():
        raise IngestError(f"input does not exist: {source}")
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise IngestError(f"unsupported input format {source.suffix or '<none>'}; use wav, mp3, m4a, or mp4")
    selected = choose_backend(backend)
    if not model:
        raise IngestError("no ASR model configured; pass --model /path/to/local/model or set TTS_ASR_MODEL")
    with tempfile.TemporaryDirectory(prefix="audio-ingest-") as temp:
        audio = _audio_for_asr(source, Path(temp))
        if selected == "mlx-whisper":
            result = transcribe_mlx(audio, model, language)
        elif selected == "faster-whisper":
            result = transcribe_faster_whisper(audio, model, language)
        elif selected == "openai-whisper":
            result = transcribe_openai_whisper(audio, model, language)
        else:
            result = transcribe_whisper_cpp(audio, model, language)
    payload = normalize_result(result, source, _duration(source), language)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="local wav/mp3/m4a/mp4 file")
    parser.add_argument("output", type=Path, help="ASR JSON output path")
    parser.add_argument("--backend", choices=("auto", "mlx-whisper", "faster-whisper", "openai-whisper", "whisper.cpp"), default="auto")
    parser.add_argument("--model", help="local model path or already-installed backend model name")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    try:
        payload = ingest(args.input, args.output, args.backend, args.model or os.getenv("TTS_ASR_MODEL"), args.language)
    except (OSError, IngestError, ImportError, RuntimeError) as exc:
        print(f"audio ingest failed: {exc}", file=sys.stderr)
        return 1
    print(f"segments={len(payload['segments'])} duration={payload['duration']:.3f}s -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
