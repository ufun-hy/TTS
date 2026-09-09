"""Timestamp-first ASR context grouping for downstream Generalize/TTS."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .engine import estimate_duration
from .sentence_reconstruction import ReconstructionConfig, reconstruct_segments


@dataclass
class ResegmentConfig:
    max_context_duration: float = 15.0
    pause_threshold: float = 0.05


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def split_text(text: str, config: ResegmentConfig) -> List[str]:
    """Simple fallback for text without timestamps.

    This is a technical size limit only. It does not try to understand sentence
    boundaries or semantics.
    """

    value = normalize_text(text)
    if not value:
        return []
    max_chars = max(1, int(config.max_context_duration * 4.5))
    return [value[index:index + max_chars] for index in range(0, len(value), max_chars)]


def _segment_id(raw: Dict[str, Any], index: int) -> str:
    return normalize_text(raw.get("id")) or f"seg_{index:04d}"


def _has_timestamps(raw: Dict[str, Any]) -> bool:
    try:
        return float(raw["end"]) > float(raw["start"]) >= 0
    except (KeyError, TypeError, ValueError):
        return False


def _fallback_text_segments(raw_segments: Sequence[Dict[str, Any]], config: ResegmentConfig) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_segments, 1):
        text = normalize_text(raw.get("text", raw.get("original_text")))
        if not text:
            raise ValueError(f"segment {index}: empty text")
        start = float(raw.get("start", result[-1]["end"] if result else 0.0))
        end = float(raw.get("end", start + max(0.01, estimate_duration(text))))
        pieces = split_text(text, config)
        if not pieces:
            continue
        weights = [max(0.01, estimate_duration(piece)) for piece in pieces]
        total = sum(weights)
        cursor = start
        for piece_index, (piece, weight) in enumerate(zip(pieces, weights), 1):
            piece_end = end if piece_index == len(pieces) else cursor + (end - start) * weight / total
            result.append({
                "id": _segment_id(raw, index) if len(pieces) == 1 else f"{_segment_id(raw, index)}_{piece_index:02d}",
                "start": cursor,
                "end": piece_end,
                "text": piece,
                "source_segment_ids": [_segment_id(raw, index)],
                "reconstructed": len(pieces) > 1,
            })
            cursor = piece_end
    return result


def validate_timeline(original: Sequence[Dict[str, Any]], children: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    errors: List[str] = []
    expected_text = "".join(normalize_text(item.get("text", item.get("original_text"))) for item in original)
    actual_text = "".join(normalize_text(item.get("text")) for item in children)
    if expected_text != actual_text:
        errors.append("text_loss")

    for previous, current in zip(children, children[1:]):
        if float(current["start"]) < float(previous["end"]) - 0.001:
            errors.append(f"overlap:{previous['id']}:{current['id']}")
        if abs(float(current["start"]) - float(previous["end"])) > 0.01:
            errors.append(f"gap:{previous['id']}:{current['id']}")

    return {
        "errors": errors,
        "text_retention": 0.0 if "text_loss" in errors else 1.0,
        "order_preserved": not any(error.startswith(("overlap", "gap")) for error in errors),
    }


def resegment(
    raw_segments: Sequence[Dict[str, Any]],
    config: Optional[ResegmentConfig] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    config = config or ResegmentConfig()
    if not raw_segments:
        return [], {
            "input_segments": 0,
            "new_segment_count": 0,
            "text_retention": 1.0,
            "order_preserved": True,
        }

    timestamped = all(isinstance(raw, dict) and _has_timestamps(raw) for raw in raw_segments)
    if timestamped:
        source_segments, reconstruction_report = reconstruct_segments(
            raw_segments,
            ReconstructionConfig(
                max_context_duration=config.max_context_duration,
                pause_threshold=config.pause_threshold,
            ),
        )
    else:
        source_segments = _fallback_text_segments(raw_segments, config)
        reconstruction_report = {
            "input_segments": len(raw_segments),
            "output_segments": len(source_segments),
            "merged_source_segments": 0,
            "text_retention": 1.0,
            "pause_boundaries": 0,
            "max_context_boundaries": 0,
        }

    children: List[Dict[str, Any]] = []
    for index, raw in enumerate(source_segments, 1):
        start = float(raw["start"])
        speech_end = float(raw["end"])
        next_start = float(source_segments[index]["start"]) if index < len(source_segments) else speech_end
        pause_after = max(0.0, next_start - speech_end)
        end = speech_end + pause_after

        children.append({
            "id": str(raw.get("id") or f"seg_{index:04d}"),
            "source_segment_ids": list(raw.get("source_segment_ids") or [str(raw.get("id") or f"seg_{index:04d}")]),
            "start": round(start, 6),
            "speech_end": round(speech_end, 6),
            "end": round(end, 6),
            "speech_duration": round(max(0.0, speech_end - start), 6),
            "pause_after": round(pause_after, 6),
            "timeline_duration": round(end - start, 6),
            "text": normalize_text(raw.get("text")),
        })

    validation = validate_timeline(raw_segments, children)
    pauses = [float(item["pause_after"]) for item in children]
    speech_durations = [float(item["speech_duration"]) for item in children]
    report = {
        "input_segments": len(raw_segments),
        "new_segment_count": len(children),
        "merged_source_segments": reconstruction_report.get("merged_source_segments", 0),
        "pause_boundaries": reconstruction_report.get("pause_boundaries", 0),
        "max_context_boundaries": reconstruction_report.get("max_context_boundaries", 0),
        "segments_with_pause": sum(value > 0 for value in pauses),
        "average_pause": mean([value for value in pauses if value > 0]) if any(value > 0 for value in pauses) else 0.0,
        "max_pause": max(pauses) if pauses else 0.0,
        "average_speech_duration": mean(speech_durations) if speech_durations else 0.0,
        "max_speech_duration": max(speech_durations) if speech_durations else 0.0,
        **validation,
    }
    return children, report


def load_input(path: Path) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    values = raw.get("segments") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        raise ValueError("input must contain a segments array")
    return values


def save_output(path: Path, source: Path, children: List[Dict[str, Any]], report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "source": str(source), "segments": children, "resegment_report": report}
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
