"""Validate human alignment decisions and export only accepted natural utterances."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import subprocess
import wave

from .transcription import sha256, union_duration, write_json


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_review(rows, sources):
    if not sources:
        raise ValueError("No original sources")
    speaker_ids = {source["speaker_id"] for source in sources}
    if len(speaker_ids) != 1:
        raise ValueError("Speakers must remain separate")
    source_map = {source["source_file"]: source for source in sources}
    if len(source_map) != len(sources):
        raise ValueError("Duplicate source_file IDs")
    ids = set()
    accepted = []
    decided_intervals = {}
    for row in rows:
        source = source_map.get(row.get("source_file"))
        if not source or row.get("speaker_id") != source["speaker_id"]:
            raise ValueError("Unknown source or mismatched speaker")
        for key in ("start_time", "end_time"):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid {key}")
        start, end = row["start_time"], row["end_time"]
        if not 0 <= start < end <= source["duration"]:
            raise ValueError("Time range lies outside original source")
        status = row.get("quality_status")
        if status not in {"accepted", "rejected", "pending"}:
            raise ValueError("Unknown quality_status")
        if row.get("text_verified") is True and (not str(row.get("reviewer") or "").strip() or not str(row.get("text") or "").strip()):
            raise ValueError("Verified text requires nonempty text and a reviewer")
        if status in {"accepted", "rejected"}:
            if not str(row.get("reviewer") or "").strip():
                raise ValueError("A quality decision requires a reviewer")
            decided_intervals.setdefault(row["source_file"], []).append((start, end))
        if status == "rejected" and not row.get("rejection_reasons"):
            raise ValueError("Rejected audio requires reasons")
        if status != "accepted":
            continue
        if not source.get("authorization"):
            raise ValueError("No authorization record for accepted source")
        if any(row.get(key) is not True for key in ("text_verified", "speaker_verified", "natural_boundary_verified")):
            raise ValueError("Accepted utterances require verified text, speaker, and natural boundaries")
        if row.get("rejection_reasons"):
            raise ValueError("Accepted audio still has rejection reasons")
        utterance_id = row.get("utterance_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", utterance_id) or utterance_id in ids:
            raise ValueError("Invalid or duplicate utterance_id")
        ids.add(utterance_id)
        accepted.append(row)
    for intervals in decided_intervals.values():
        ordered = sorted(intervals)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] < previous[1]:
                raise ValueError("Overlapping quality decisions must be reconciled before export")
    return accepted


def statistics(rows, sources):
    accepted = validate_review(rows, sources)
    def covered(predicate):
        return sum(union_duration([(row["start_time"], row["end_time"]) for row in rows
                                   if row["source_file"] == source["source_file"] and predicate(row)])
                   for source in sources)
    raw = sum(source["duration"] for source in sources)
    usable = covered(lambda row: row["quality_status"] == "accepted")
    rejected = covered(lambda row: row["quality_status"] == "rejected")
    durations = [row["end_time"] - row["start_time"] for row in accepted]
    return {"raw_audio_duration": raw,
            "transcript_covered_duration": covered(lambda row: row.get("text_verified") is True),
            "usable_audio_duration": usable, "rejected_duration": rejected,
            "pending_duration": max(0, raw - usable - rejected), "segments": len(accepted),
            "average_duration": sum(durations) / len(durations) if durations else None,
            "min_duration": min(durations) if durations else None,
            "max_duration": max(durations) if durations else None,
            "scope": "confirmed decisions only; unreviewed recording remains pending"}


def export_review(speaker_dir: Path, alignment_path: Path, output: Path):
    sources = read_jsonl(speaker_dir / "sources.jsonl")
    rows = read_jsonl(alignment_path)
    accepted = validate_review(rows, sources)
    if not accepted:
        raise ValueError("No fully reviewed accepted utterances; no dataset exported")
    if output.exists():
        raise ValueError("Output already exists; use a new export directory to preserve previous results")
    for source in sources:
        if not Path(source["original_path"]).is_file():
            raise ValueError("Original recording is missing")
        if source.get("sha256") and sha256(Path(source["original_path"])) != source["sha256"]:
            raise ValueError("Original recording changed since inventory")
    output.mkdir(parents=True)
    (output / "audio").mkdir()
    write_json(output / "export-status.json", {"state": "running", "alignment_sha256": sha256(alignment_path)})
    by_file = {source["source_file"]: source for source in sources}
    manifest = []
    try:
        for row in accepted:
            relative = f"audio/{row['utterance_id']}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-ss", str(row["start_time"]),
                            "-i", by_file[row["source_file"]]["original_path"],
                            "-t", str(row["end_time"] - row["start_time"]),
                            "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(output / relative)], check=True)
            with wave.open(str(output / relative), "rb") as audio:
                actual_duration = audio.getnframes() / audio.getframerate()
                if abs(actual_duration - (row["end_time"] - row["start_time"])) > 1 / 24000 + 1e-6:
                    raise ValueError("Decoded utterance duration does not match reviewed boundaries")
            manifest.append({"utterance_id": row["utterance_id"], "audio_path": relative,
                             "text": row["text"], "speaker_id": row["speaker_id"],
                             "start_time": row["start_time"], "end_time": row["end_time"],
                             "duration": row["end_time"] - row["start_time"], "source_file": row["source_file"]})
        (output / "manifest.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in manifest), encoding="utf-8")
        (output / "alignment.jsonl").write_bytes(alignment_path.read_bytes())
        write_json(output / "statistics.json", statistics(rows, sources))
        write_json(output / "export-status.json", {"state": "complete", "utterances": len(manifest),
                                                 "alignment_sha256": sha256(alignment_path)})
    except Exception as exc:
        write_json(output / "export-status.json", {"state": "failed", "error": str(exc)})
        raise
    return manifest
