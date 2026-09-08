"""Rule-first reconstruction of short timestamped ASR fragments."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Sequence, Tuple

from .semantic_boundary import boundary_type, classify, is_entity_boundary


STRONG_END = re.compile(r"[。！？!?；;]$")
QUESTION_END = re.compile(r"[？?]$")


@dataclass
class ReconstructionConfig:
    preferred_min: float = 6.0
    preferred_max: float = 15.0
    max_duration: float = 25.0
    min_duration: float = 3.0
    pause_candidate: float = 0.3
    pause_priority: float = 0.8
    hard_pause: float = 1.5


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _segment_id(raw: Dict[str, Any], index: int) -> str:
    return _text(raw.get("id")) or f"asr_{index:04d}"


def _join(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    return left + right


def _duration(items: Sequence[Dict[str, Any]]) -> float:
    if not items:
        return 0.0
    return max(0.0, _number(items[-1].get("end")) - _number(items[0].get("start")))


def _gap(left: Dict[str, Any], right: Dict[str, Any]) -> float:
    return max(0.0, _number(right.get("start")) - _number(left.get("end")))


def _timestamped(raw: Dict[str, Any]) -> bool:
    return bool(raw.get("words") or raw.get("sentences"))


def _semantic_cut(left: str, right: str, current_duration: float) -> bool:
    boundary = boundary_type(left, right)
    if not is_entity_boundary(left, right):
        return False
    left_type = classify(left)
    right_type = classify(right)
    if right_type in {"price", "promotion"} and left_type not in {"interaction", "cta", "promotion"}:
        return current_duration >= 1.0
    return bool(boundary and current_duration >= 4.0)


def _independent_short(text: str) -> bool:
    return classify(text) in {"price", "promotion"} or bool(QUESTION_END.search(text))


def _should_cut(current: Sequence[Dict[str, Any]], following: Dict[str, Any], config: ReconstructionConfig) -> bool:
    left_text = "".join(_text(item.get("text")) for item in current)
    right_text = _text(following.get("text"))
    current_duration = _duration(current)
    combined_duration = max(0.0, _number(following.get("end")) - _number(current[0].get("start")))
    gap = _gap(current[-1], following)
    strong = bool(STRONG_END.search(left_text))
    semantic = _semantic_cut(left_text, right_text, current_duration)

    if gap >= config.hard_pause or combined_duration > config.max_duration:
        return True
    if current_duration >= 20.0:
        return True
    if semantic:
        return True
    if current_duration < config.min_duration:
        return _independent_short(left_text) and gap >= config.pause_priority
    if gap >= config.pause_priority and current_duration >= 4.0:
        return True
    if strong and current_duration >= config.preferred_min:
        return True
    if current_duration >= config.preferred_min and combined_duration > config.preferred_max:
        return True
    return False


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
        "reconstruction_source": ["asr_merge"] if len(source_ids) > 1 else [],
    }
    words = [word for item in items for word in item.get("words", []) if isinstance(word, dict)]
    if words:
        result["words"] = words
    sentences = [sentence for item in items for sentence in item.get("sentences", []) if isinstance(sentence, dict)]
    if sentences and not words:
        result["sentences"] = sentences
    return result


def reconstruct_segments(raw_segments: Sequence[Dict[str, Any]], config: ReconstructionConfig | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Merge timestamped ASR fragments without changing their text."""
    config = config or ReconstructionConfig()
    if not raw_segments:
        return [], {"input_segments": 0, "output_segments": 0, "merged_segments": 0, "text_retention": 1.0}
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise ValueError("ASR segment must be an object")
        if not _text(raw.get("text", raw.get("original_text"))):
            raise ValueError("ASR segment text must not be empty")
        if current and _should_cut(current, raw, config):
            groups.append(current)
            current = []
        current.append(raw)
    if current:
        groups.append(current)

    reconstructed = [_merge_group(group, index) for index, group in enumerate(groups, 1)]
    for index in range(len(groups) - 1):
        left_text = reconstructed[index]["text"]
        right_text = _text(groups[index + 1][0].get("text", groups[index + 1][0].get("original_text")))
        if _semantic_cut(left_text, right_text, _duration(groups[index])):
            reconstructed[index]["reconstruction_source"].append("semantic_rule")
    original_text = "".join(_text(item.get("text", item.get("original_text"))) for item in raw_segments)
    output_text = "".join(item["text"] for item in reconstructed)
    if original_text != output_text:
        raise ValueError("sentence reconstruction changed ASR text")
    durations = [max(0.0, item["end"] - item["start"]) for item in reconstructed]
    boundary_counts: Dict[str, int] = {}
    for item in reconstructed:
        for source in item["reconstruction_source"]:
            boundary_counts[source] = boundary_counts.get(source, 0) + 1
    report = {
        "input_segments": len(raw_segments),
        "output_segments": len(reconstructed),
        "merged_segments": sum(len(item["source_segment_ids"]) > 1 for item in reconstructed),
        "merged_source_segments": sum(max(0, len(item["source_segment_ids"]) - 1) for item in reconstructed),
        "text_retention": 1.0,
        "average_duration": sum(durations) / len(durations) if durations else 0.0,
        "p50_duration": _percentile(durations, 0.5),
        "p90_duration": _percentile(durations, 0.9),
        "max_duration": max(durations) if durations else 0.0,
        "boundary_source_counts": boundary_counts,
    }
    return reconstructed, report


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
