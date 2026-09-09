"""Checkpointed local ASR. Decoder windows are NOT training utterances."""

from __future__ import annotations

import hashlib
import importlib.util
from importlib.metadata import version
import json
import math
from pathlib import Path
import subprocess
import time


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def windows(duration: float, size: float, overlap: float):
    if not all(math.isfinite(v) for v in (duration, size, overlap)):
        raise ValueError("window values must be finite")
    if duration <= 0 or size <= 0 or not 0 <= overlap < size / 2:
        raise ValueError("invalid duration/window/overlap")
    for index in range(math.ceil(duration / size)):
        start = index * size
        end = min(duration, start + size)
        yield {"index": index, "core_start": start, "core_end": end,
               "decode_start": max(0, start - overlap), "decode_end": min(duration, end + overlap)}


def union_duration(intervals) -> float:
    total = 0.0
    left = right = None
    for start, end in sorted(intervals):
        if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
            raise ValueError("invalid time interval")
        if left is None:
            left, right = start, end
        elif start > right:
            total += right - left
            left, right = start, end
        else:
            right = max(right, end)
    return total + (right - left if left is not None else 0)


def candidate_segments(checkpoint, source, source_duration):
    window = checkpoint["window"]
    offset = window["decode_start"]
    result = []
    for index, segment in enumerate(checkpoint["result"].get("segments", [])):
        start = offset + float(segment["start"])
        end = offset + float(segment["end"])
        if not (math.isfinite(start) and math.isfinite(end)):
            raise ValueError("non-finite ASR timestamps")
        # One core owns each midpoint; overlapping decoder context stays in the raw checkpoint.
        if not window["core_start"] <= (start + end) / 2 < window["core_end"]:
            continue
        text = str(segment.get("text", "")).strip()
        flags = []
        if start < 0 or end > source_duration or end <= start:
            flags.append("invalid_timing")
        if start <= window["core_start"] or end >= window["core_end"]:
            flags.append("window_seam_review")
        if float(segment.get("no_speech_prob", 0)) > 0.6:
            flags.append("asr_no_speech_probability")
        if float(segment.get("avg_logprob", 0)) < -1:
            flags.append("asr_low_confidence")
        if float(segment.get("compression_ratio", 0)) > 2.4:
            flags.append("asr_repetition")
        words = []
        for word in segment.get("words", []):
            item = dict(word)
            item["start"] = offset + float(item["start"])
            item["end"] = offset + float(item["end"])
            words.append(item)
            if not (math.isfinite(item["start"]) and math.isfinite(item["end"])):
                raise ValueError("non-finite ASR word timestamps")
            if item["end"] <= item["start"] or item["start"] < start or item["end"] > end:
                flags.append("word_timing_review")
        if not text:
            flags.append("empty_text")
        result.append({
            "candidate_id": f"{source['speaker_id']}_w{window['index']:04d}_s{index:04d}",
            "source_file": source["source_file"], "speaker_id": source["speaker_id"],
            "start_time": start, "end_time": end, "duration": end - start,
            "text": text, "text_source": "mlx-whisper candidate; NOT verified truth",
            "text_verified": False, "speaker_verified": False, "natural_boundary_verified": False,
            "quality_status": "pending", "reviewer": None, "review_flags": sorted(set(flags)),
            "avg_logprob": segment.get("avg_logprob"), "words": words,
        })
    return result


def assemble(asr_dir: Path, source: dict, all_windows: list) -> dict:
    candidates = []
    completed = []
    for window in all_windows:
        path = asr_dir / f"window-{window['index']:04d}.json"
        if path.exists():
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            candidates.extend(candidate_segments(checkpoint, source, source["duration"]))
            completed.append((window["core_start"], window["core_end"]))
    candidates.sort(key=lambda item: (item["start_time"], item["end_time"]))
    for previous, current in zip(candidates, candidates[1:]):
        if current["start_time"] < previous["end_time"]:
            previous["review_flags"].append("candidate_overlap_review")
            current["review_flags"].append("candidate_overlap_review")
    # Rebuild ASR candidates only; never overwrite separately saved human alignment decisions.
    output = asr_dir / "candidates.jsonl"
    temporary = output.with_suffix(".jsonl.tmp")
    temporary.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates), encoding="utf-8")
    temporary.replace(output)
    valid = [(max(0, c["start_time"]), min(source["duration"], c["end_time"])) for c in candidates
             if 0 <= c["start_time"] < c["end_time"] <= source["duration"]]
    status = {
        "state": "complete_pending_review" if len(completed) == len(all_windows) else "running",
        "completed_windows": len(completed), "total_windows": len(all_windows),
        "processed_audio_duration": union_duration(completed),
        "candidate_speech_duration": union_duration(valid), "candidate_count": len(candidates),
        "reliable_transcript_covered_duration": None,
        "usable_audio_duration": None,
        "note": "ASR intervals are pending hypotheses, not accepted training slices; overlaps and gaps require review.",
    }
    write_json(asr_dir / "status.json", status)
    return status


def prepare_speaker(speaker_dir: Path, model: Path, transcribe, size=600.0, overlap=5.0):
    sources = [json.loads(line) for line in (speaker_dir / "sources.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(sources) != 1:
        raise ValueError("This batch entry expects exactly one original recording per speaker")
    source = sources[0]
    original = Path(source["original_path"])
    asr_dir = speaker_dir / "asr"
    asr_dir.mkdir(exist_ok=True)
    identity = {"source_sha256": sha256(original), "duration": source["duration"],
                "model_path": str(model.resolve()), "model_sha256": sha256(model / "weights.safetensors"),
                "model_config_sha256": sha256(model / "config.json"), "backend_version": version("mlx-whisper"),
                "window_seconds": size, "overlap_seconds": overlap, "language": "zh",
                "word_timestamps": True, "backend": "mlx-whisper"}
    identity_path = asr_dir / "identity.json"
    if identity_path.exists():
        if json.loads(identity_path.read_text(encoding="utf-8")) != identity:
            raise ValueError("Source/model/settings changed; retain the existing ASR and use a new run directory")
    else:
        write_json(identity_path, identity)
    all_windows = list(windows(source["duration"], size, overlap))
    for window in all_windows:
        checkpoint = asr_dir / f"window-{window['index']:04d}.json"
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if saved["window"] != window or saved["identity"] != identity:
                raise ValueError(f"Checkpoint mismatch: {checkpoint}")
            continue
        wave = asr_dir / f"window-{window['index']:04d}.wav"
        # Keep decoded work audio; no automatic deletion of user-related data.
        if not wave.exists():
            partial_wave = wave.with_suffix(".partial.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", str(window["decode_start"]),
                            "-i", str(original), "-t", str(window["decode_end"] - window["decode_start"]),
                            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(partial_wave)], check=True)
            partial_wave.replace(wave)
        began = time.monotonic()
        print(json.dumps({"speaker": source["speaker_id"], "window": window["index"], "state": "transcribing"}), flush=True)
        result = transcribe(wave, str(model.resolve()), "zh")
        write_json(checkpoint, {"identity": identity, "window": window,
                               "elapsed_seconds": time.monotonic() - began, "result": result})
        status = assemble(asr_dir, source, all_windows)
        print(json.dumps({"speaker": source["speaker_id"], **status}), flush=True)
    return assemble(asr_dir, source, all_windows)


def local_backend(project_root: Path):
    # Reuse the existing local MLX adapter, keeping its strict training normalizer out of candidate ASR.
    spec = importlib.util.spec_from_file_location("audio_ingest", project_root / "scripts/audio-ingest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.transcribe_mlx
