"""Timestamp-first context grouping for ASR fragments."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
import re
from typing import Any, Dict, List, Sequence, Tuple


@dataclass
class ReconstructionConfig:
    """Only two things may create a new context block.

    1. A real timestamp gap (pause) at or above ``pause_threshold``.
    2. The current continuous block would exceed ``max_context_duration``.

    No punctuation, semantic category, price/CTA keywords, or content judge is used.
    """

    max_context_duration: float = 15.0
    pause_threshold: float = 0.05


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _segment_id(raw: Dict[str, Any], index: int) -> str:
    return _text(raw.get("id")) or f"asr_{index:04d}"


def _gap(left: Dict[str, Any], right: Dict[str, Any]) -> float:
    return max(0.0, _number(right.get("start")) - _number(left.get("end")))


def _context_span(current: Sequence[Dict[str, Any]], following: Dict[str, Any]) -> float:
    if not current:
        return 0.0
    return max(0.0, _number(following.get("end")) - _number(current[0].get("start")))


def _cut_reason(
    current: Sequence[Dict[str, Any]],
    following: Dict[str, Any],
    config: ReconstructionConfig,
) -> str | None:
    if not current:
        return None
    if _gap(current[-1], following) >= config.pause_threshold:
        return "pause"
    if _context_span(current, following) > config.max_context_duration:
        return "max_context"
    return None


def _merge_group(items: Sequence[Dict[str, Any]], index: int) -> Dict[str, Any]:
    source_ids = [_segment_id(item, item_index) for item_index, item in enumerate(items, 1)]
    text = "".join(_text(item.get("text", item.get("original_text"))) for item in items)
    result: Dict[str, Any] = {
        "id": source_ids[0] if len(source_ids) == 1 else f"recon_{index:04d}",
        "start": _number(items[0].get("start")),
        "end": _number(items[-1].get("end")),
        "text": text,
        "source_segment_ids": source_ids,
        "reconstructed": len(source_ids) > 1,
    }
    words = [word for item in items for word in item.get("words", []) if isinstance(word, dict)]
    if words:
        result["words"] = words
    return result


def reconstruct_segments(
    raw_segments: Sequence[Dict[str, Any]],
    config: ReconstructionConfig | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Merge ASR fragments by timestamp only, without changing their text."""

    config = config or ReconstructionConfig()
    if config.max_context_duration <= 0:
        raise ValueError("max_context_duration must be positive")
    if config.pause_threshold < 0:
        raise ValueError("pause_threshold must be non-negative")
    if not raw_segments:
        return [], {
            "input_segments": 0,
            "output_segments": 0,
            "merged_source_segments": 0,
            "text_retention": 1.0,
            "pause_boundaries": 0,
            "max_context_boundaries": 0,
        }

    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    pause_boundaries = 0
    max_context_boundaries = 0

    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise ValueError("ASR segment must be an object")
        if not _text(raw.get("text", raw.get("original_text"))):
            raise ValueError("ASR segment text must not be empty")
        start = _number(raw.get("start"), -1.0)
        end = _number(raw.get("end"), -1.0)
        if start < 0 or end <= start:
            raise ValueError("ASR segment needs valid start/end timestamps")

        reason = _cut_reason(current, raw, config)
        if reason:
            groups.append(current)
            current = []
            if reason == "pause":
                pause_boundaries += 1
            else:
                max_context_boundaries += 1
        current.append(raw)

    if current:
        groups.append(current)

    reconstructed = [_merge_group(group, index) for index, group in enumerate(groups, 1)]

    original_text = "".join(_text(item.get("text", item.get("original_text"))) for item in raw_segments)
    output_text = "".join(item["text"] for item in reconstructed)
    if original_text != output_text:
        raise ValueError("sentence reconstruction changed ASR text")

    durations = [max(0.0, item["end"] - item["start"]) for item in reconstructed]
    return reconstructed, {
        "input_segments": len(raw_segments),
        "output_segments": len(reconstructed),
        "merged_source_segments": len(raw_segments) - len(reconstructed),
        "text_retention": 1.0,
        "average_context_duration": mean(durations) if durations else 0.0,
        "max_context_duration": max(durations) if durations else 0.0,
        "pause_boundaries": pause_boundaries,
        "max_context_boundaries": max_context_boundaries,
    }
