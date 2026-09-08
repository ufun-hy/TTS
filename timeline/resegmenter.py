"""Rule-first semantic re-segmentation for coarse ASR timelines."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .engine import estimate_duration
from .semantic_boundary import boundary_type, classify, is_entity_boundary


@dataclass
class ResegmentConfig:
    max_duration: float = 25.0
    preferred_max: float = 15.0
    min_duration: float = 3.0
    pause_threshold: float = 0.5


@dataclass
class Unit:
    text: str
    start: Optional[float] = None
    end: Optional[float] = None
    semantic_type: str = "other"
    semantic_boundary: Optional[str] = None
    boundary_source: List[str] = field(default_factory=list)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _duration(text: str) -> float:
    return max(0.01, estimate_duration(text))


def _split_at_punctuation(text: str) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []
    pieces = []
    start = 0
    for match in re.finditer(r"[。！？!?；;，,]", text):
        end = match.end()
        pieces.append(text[start:end])
        start = end
    if start < len(text):
        pieces.append(text[start:])
    return [piece for piece in pieces if piece]


def _split_long(text: str, max_duration: float) -> List[str]:
    pieces = []
    for sentence in _split_at_punctuation(text):
        if _duration(sentence) <= max_duration:
            pieces.append(sentence)
            continue
        comma_parts = [part for part in re.split(r"(?<=[，,、：:])", sentence) if part]
        if len(comma_parts) == 1:
            max_chars = max(20, int(max_duration * 4.5))
            comma_parts = [sentence[index:index + max_chars] for index in range(0, len(sentence), max_chars)]
        for part in comma_parts:
            if _duration(part) <= max_duration:
                pieces.append(part)
            else:
                max_chars = max(20, int(max_duration * 4.5))
                pieces.extend(part[index:index + max_chars] for index in range(0, len(part), max_chars))
    return [piece for piece in pieces if normalize_text(piece)]


def _pack_text_units(texts: Sequence[str], config: ResegmentConfig) -> List[Unit]:
    """Pack complete punctuation units into useful speech-sized chunks."""
    packed: List[Unit] = []
    current: Optional[Unit] = None
    for text in texts:
        text = normalize_text(text)
        if not text:
            continue
        next_unit = Unit(text, semantic_type=classify(text), boundary_source=["punctuation"])
        if current is None:
            current = next_unit
            continue
        combined = current.text + text
        boundary = boundary_type(current.text, text)
        if boundary and _duration(current.text) >= max(2.0, config.min_duration * 0.7) and is_entity_boundary(current.text, text):
            current.semantic_boundary = boundary
            current.boundary_source.append("semantic_rule")
            packed.append(current)
            current = next_unit
        elif _duration(current.text) < config.min_duration and _duration(combined) <= config.max_duration:
            current.text = combined
            current.semantic_type = classify(combined)
        elif _duration(combined) <= config.preferred_max:
            current.text = combined
            current.semantic_type = classify(combined)
        else:
            packed.append(current)
            current = next_unit
    if current is not None:
        packed.append(current)
    return packed


def _pack_units(texts: Sequence[str], config: ResegmentConfig) -> List[str]:
    return [unit.text for unit in _pack_text_units(texts, config)]


def _split_text_units(text: str, config: ResegmentConfig) -> List[Unit]:
    base: List[str] = []
    for sentence in _split_at_punctuation(text):
        base.extend(_split_long(sentence, config.max_duration))
    units = _pack_text_units(base, config)
    if len(units) > 1 and not re.search(r"[。！？!?；;，,]", text):
        for unit in units:
            unit.boundary_source.append("char_fallback")
    return units


def split_text(text: str, config: ResegmentConfig) -> List[str]:
    return [unit.text for unit in _split_text_units(text, config)]


def _timestamped_units(raw: Dict[str, Any], config: ResegmentConfig) -> Optional[List[Unit]]:
    values = raw.get("sentences")
    if isinstance(values, list) and values:
        result = []
        for value in values:
            if not isinstance(value, dict) or not normalize_text(value.get("text")):
                continue
            try:
                result.append(Unit(normalize_text(value["text"]), float(value["start"]), float(value["end"]), classify(normalize_text(value["text"])), boundary_source=["timestamp"]))
            except (KeyError, TypeError, ValueError):
                return None
        return result or None

    words = raw.get("words")
    if not isinstance(words, list) or not words:
        return None
    result: List[Unit] = []
    current: List[str] = []
    current_start: Optional[float] = None
    previous_end: Optional[float] = None
    for value in words:
        if not isinstance(value, dict):
            continue
        word = normalize_text(value.get("word", value.get("text")))
        if not word:
            continue
        try:
            start = float(value["start"])
            end = float(value["end"])
        except (KeyError, TypeError, ValueError):
            return None
        gap = start - previous_end if previous_end is not None else 0
        if current and (gap >= config.pause_threshold or re.search(r"[。！？!?；;]$", current[-1])):
            result.append(Unit("".join(current), current_start, previous_end, classify("".join(current)), boundary_source=["timestamp"]))
            current = []
            current_start = None
        if current_start is None:
            current_start = start
        current.append(word)
        previous_end = end
        if re.search(r"[。！？!?；;]$", word):
            result.append(Unit("".join(current), current_start, end, classify("".join(current)), boundary_source=["timestamp"]))
            current = []
            current_start = None
    if current:
        result.append(Unit("".join(current), current_start, previous_end, classify("".join(current)), boundary_source=["timestamp"]))
    return result or None


def _split_timestamped(unit: Unit, config: ResegmentConfig) -> List[Unit]:
    if unit.start is None or unit.end is None or unit.end <= unit.start:
        return _split_text_units(unit.text, config)
    if unit.end - unit.start <= config.max_duration:
        return [unit]
    text_units = _split_text_units(unit.text, config)
    weights = [_duration(item.text) for item in text_units]
    total = sum(weights) or 1
    result = []
    cursor = unit.start
    for index, (item, weight) in enumerate(zip(text_units, weights)):
        end = unit.end if index == len(text_units) - 1 else cursor + (unit.end - unit.start) * weight / total
        item.start = cursor
        item.end = end
        item.boundary_source = list(dict.fromkeys(item.boundary_source + ["timestamp"]))
        result.append(item)
        cursor = end
    return result


def _estimate_times(texts: Sequence[str], start: float, end: float) -> List[Unit]:
    weights = [_duration(text) for text in texts]
    total = sum(weights) or 1
    result = []
    cursor = start
    for index, (text, weight) in enumerate(zip(texts, weights)):
        child_end = end if index == len(texts) - 1 else cursor + (end - start) * weight / total
        result.append(Unit(text, cursor, child_end))
        cursor = child_end
    return result


def _estimate_unit_times(units: Sequence[Unit], start: float, end: float) -> List[Unit]:
    weights = [_duration(item.text) for item in units]
    total = sum(weights) or 1
    result = []
    cursor = start
    for index, (item, weight) in enumerate(zip(units, weights)):
        child_end = end if index == len(units) - 1 else cursor + (end - start) * weight / total
        item.start = cursor
        item.end = child_end
        result.append(item)
        cursor = child_end
    return result


def _pause_class(value: float) -> str:
    if value < 0.2:
        return "micro_pause"
    if value < 0.5:
        return "normal_pause"
    if value <= 1.2:
        return "semantic_pause"
    return "long_pause"


def _mark_semantic_transitions(units: List[Unit]) -> None:
    for left, right in zip(units, units[1:]):
        boundary = boundary_type(left.text, right.text)
        if boundary and is_entity_boundary(left.text, right.text):
            left.semantic_boundary = boundary
            left.boundary_source = list(dict.fromkeys(left.boundary_source + ["semantic_rule"]))


def _segment_id(raw: Dict[str, Any], index: int) -> str:
    return normalize_text(raw.get("id")) or f"seg_{index:03d}"


def resegment_segment(raw: Dict[str, Any], index: int, config: ResegmentConfig) -> List[Dict[str, Any]]:
    parent_id = _segment_id(raw, index)
    text = normalize_text(raw.get("text", raw.get("original_text")))
    if not text:
        raise ValueError(f"{parent_id}: empty text")
    try:
        start = float(raw["start"])
        end = float(raw["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{parent_id}: invalid start/end") from exc
    if end <= start:
        raise ValueError(f"{parent_id}: end must be greater than start")

    timestamped = _timestamped_units(raw, config)
    if timestamped:
        units = [child for unit in timestamped for child in _split_timestamped(unit, config)]
        _mark_semantic_transitions(units)
    else:
        units = _estimate_unit_times(_split_text_units(text, config), start, end)
    if not units:
        units = [Unit(text, start, end)]

    normalized_parent = normalize_text(text)
    normalized_children = normalize_text("".join(unit.text for unit in units))
    if normalized_parent != normalized_children:
        raise ValueError(f"{parent_id}: child text does not reconstruct parent text")

    result = []
    suffix = len(units) > 1
    for child_index, unit in enumerate(units, 1):
        child_id = f"{parent_id}_{child_index:02d}" if suffix else parent_id
        child_start = start if child_index == 1 else float(unit.start if unit.start is not None else result[-1]["end"])
        speech_end = float(unit.end or child_start)
        next_start = float(units[child_index].start) if child_index < len(units) and units[child_index].start is not None else end
        pause_after = max(0.0, next_start - speech_end) if timestamped else 0.0
        child_end = end if child_index == len(units) else speech_end + pause_after
        if child_end < speech_end:
            child_end = speech_end
        boundary_source = list(unit.boundary_source)
        if timestamped and pause_after > 0:
            boundary_source.append("pause")
        result.append({
            "id": child_id,
            "parent_segment_id": parent_id,
            "source_segment_id": parent_id,
            "source_start": start,
            "source_end": end,
            "start": round(child_start, 6),
            "speech_end": round(speech_end, 6),
            "end": round(child_end, 6),
            "speech_duration": round(max(0.0, speech_end - child_start), 6),
            "pause_after": round(pause_after, 6),
            "timeline_duration": round(child_end - child_start, 6),
            "duration": round(child_end - child_start, 6),
            "semantic_type": unit.semantic_type,
            "semantic_boundary": unit.semantic_boundary,
            "boundary_source": list(dict.fromkeys(boundary_source)),
            "text": unit.text,
        })
    return result


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def validate_timeline(original: Sequence[Dict[str, Any]], children: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    errors = []
    if not original or not children:
        errors.append("empty_timeline")
    original_start = float(original[0]["start"]) if original else 0.0
    original_end = float(original[-1]["end"]) if original else 0.0
    if children and abs(float(children[0]["start"]) - original_start) > 0.001:
        errors.append("global_start_changed")
    if children and abs(float(children[-1]["end"]) - original_end) > 0.001:
        errors.append("global_end_changed")
    for previous, current in zip(children, children[1:]):
        if float(current["start"]) < float(previous["end"]) - 0.001:
            errors.append(f"overlap:{previous['id']}:{current['id']}")
        if abs(float(current["start"]) - float(previous["end"])) > 0.01:
            errors.append(f"gap:{previous['id']}:{current['id']}")
    for index, parent in enumerate(original, 1):
        parent_id = _segment_id(parent, index)
        expected = normalize_text(parent.get("text", parent.get("original_text")))
        actual = normalize_text("".join(item["text"] for item in children if item["parent_segment_id"] == parent_id))
        if expected != actual:
            errors.append(f"text_loss:{parent_id}")
    texts = [item["text"] for item in children]
    duplicate_count = sum(left == right for left, right in zip(texts, texts[1:]) if left)
    durations = [float(item["end"]) - float(item["start"]) for item in children]
    return {
        "errors": errors,
        "text_retention": 0.0 if any(error.startswith("text_loss") for error in errors) else 1.0,
        "order_preserved": not any(error.startswith(("overlap", "gap")) for error in errors),
        "timeline_start_end_preserved": not any(error in {"global_start_changed", "global_end_changed"} for error in errors),
        "duplicate_adjacent_text_count": duplicate_count,
        "average_duration": mean(durations) if durations else 0.0,
        "p50_duration": _percentile(durations, 0.50),
        "p90_duration": _percentile(durations, 0.90),
        "p95_duration": _percentile(durations, 0.95),
        "max_duration": max(durations) if durations else 0.0,
    }


def resegment(raw_segments: Sequence[Dict[str, Any]], config: Optional[ResegmentConfig] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    config = config or ResegmentConfig()
    children = []
    rule_fallback_count = 0
    for index, raw in enumerate(raw_segments, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"segment {index}: expected object")
        if not _timestamped_units(raw, config):
            rule_fallback_count += 1
        children.extend(resegment_segment(raw, index, config))
    validation = validate_timeline(raw_segments, children)
    original_durations = [float(item["end"]) - float(item["start"]) for item in raw_segments]
    child_durations = [float(item["end"]) - float(item["start"]) for item in children]
    pauses = [float(item.get("pause_after", 0.0)) for item in children]
    report = {
        "original_segment_count": len(raw_segments),
        "new_segment_count": len(children),
        "original_average_duration": mean(original_durations) if original_durations else 0.0,
        "original_p50_duration": _percentile(original_durations, 0.50),
        "original_p90_duration": _percentile(original_durations, 0.90),
        "original_p95_duration": _percentile(original_durations, 0.95),
        "original_max_duration": max(original_durations) if original_durations else 0.0,
        "new_average_duration": mean(child_durations) if child_durations else 0.0,
        "new_max_duration": max(child_durations) if child_durations else 0.0,
        "original_over_20s": sum(value > 20 for value in original_durations),
        "original_over_25s": sum(value > 25 for value in original_durations),
        "original_over_30s": sum(value > 30 for value in original_durations),
        "new_over_20s": sum(value > 20 for value in child_durations),
        "new_over_25s": sum(value > 25 for value in child_durations),
        "new_over_30s": sum(value > 30 for value in child_durations),
        "llm_assisted_splits": 0,
        "llm_timeout_count": 0,
        "rule_fallback_count": rule_fallback_count,
        "semantic_rule_split_count": sum("semantic_rule" in item.get("boundary_source", []) for item in children),
        "pause_based_split_count": sum("pause" in item.get("boundary_source", []) for item in children),
        "char_fallback_count": sum("char_fallback" in item.get("boundary_source", []) for item in children),
        "segments_with_pause": sum(value > 0 for value in pauses),
        "average_pause": mean(pauses) if pauses else 0.0,
        "p50_pause": _percentile(pauses, 0.50),
        "p90_pause": _percentile(pauses, 0.90),
        "max_pause": max(pauses) if pauses else 0.0,
        **validation,
    }
    return children, report


def load_input(path: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".txt":
        text = path.read_text(encoding="utf-8")
        paragraphs = [normalize_text(value) for value in re.split(r"\n\s*\n", text) if normalize_text(value)]
        if not paragraphs:
            paragraphs = [normalize_text(value) for value in text.splitlines() if normalize_text(value)]
        result = []
        cursor = 0.0
        for index, paragraph in enumerate(paragraphs, 1):
            duration = _duration(paragraph)
            result.append({"id": f"seg_{index:03d}", "start": cursor, "end": cursor + duration, "text": paragraph})
            cursor += duration
        return result
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
