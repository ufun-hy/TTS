"""Runtime-only selection of already generated timeline candidates."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import random
from typing import Any, Dict, List, Sequence, Tuple

from .engine import estimate_duration
from .session import RuntimeSession


@dataclass
class Selection:
    segment_id: str
    mode: str
    variant_ids: List[str]
    combination_id: str
    final_text: str
    estimated_duration: float
    target_duration: float
    fallback: bool = False


def _duration_score(text: str, target: float) -> float:
    return abs(estimate_duration(text) - target)


class RuntimeSelector:
    def __init__(self, cooldown_window: int = 3, tolerance: float = 0.15, seed: int | None = None) -> None:
        self.cooldown_window = cooldown_window
        self.tolerance = tolerance
        self.random = random.Random(seed)

    def select(self, segment: Dict[str, Any], session: RuntimeSession) -> Selection:
        segment_id = str(segment["id"])
        mode = str(segment.get("mode", "atomic"))
        target = float(segment.get("speech_duration", segment.get("duration_target", float(segment["end"]) - float(segment["start"]))))
        if mode in {"composable", "compact_composable"}:
            result = self._select_composable(segment, session, target)
        else:
            result = self._select_atomic(segment, session, target)
        session.remember(segment_id, result.variant_ids, result.combination_id, self.cooldown_window)
        return result

    def _select_atomic(self, segment: Dict[str, Any], session: RuntimeSession, target: float) -> Selection:
        segment_id = str(segment["id"])
        candidates = [(f"candidate_{index + 1:02d}", str(value).strip()) for index, value in enumerate(segment.get("candidates", [])) if str(value).strip()]
        if candidates:
            recent = set(session.recent_variants.get(segment_id, []))
            available = [item for item in candidates if item[0] not in recent] or candidates
            available.sort(key=lambda item: _duration_score(item[1], target))
            variant_id, text = self.random.choice(available[: min(3, len(available))])
            return Selection(segment_id, str(segment.get("strategy", "simple_candidate")), [variant_id], variant_id, text, estimate_duration(text), target)
        values = [str(value).strip() for value in segment.get("variants", []) if str(value).strip()]
        recent = set(session.recent_variants.get(segment_id, []))
        candidates = [(f"variant_{index + 1}", value) for index, value in enumerate(values)]
        available = [(key, value) for key, value in candidates if key not in recent] or candidates
        if not available:
            return self._fallback(segment, target)
        available.sort(key=lambda item: _duration_score(item[1], target))
        shortlist = available[: min(3, len(available))]
        variant_id, text = self.random.choice(shortlist)
        return Selection(segment_id, "atomic", [variant_id], variant_id, text, estimate_duration(text), target)

    def _select_composable(self, segment: Dict[str, Any], session: RuntimeSession, target: float) -> Selection:
        segment_id = str(segment["id"])
        candidates = [(f"candidate_{index + 1}", str(value).strip()) for index, value in enumerate(segment.get("candidates", [])) if str(value).strip()]
        if candidates:
            recent = set(session.recent_variants.get(segment_id, []))
            available = [item for item in candidates if item[0] not in recent] or candidates
            available.sort(key=lambda item: _duration_score(item[1], target))
            variant_id, text = available[0]
            return Selection(segment_id, str(segment.get("strategy", "composable")), [variant_id], variant_id, text, estimate_duration(text), target)
        recent = set(session.recent_variants.get(segment_id, []))
        recent_combinations = set(session.recent_combinations.get(segment_id, []))
        required: List[Tuple[str, List[Tuple[str, str]]]] = []
        optional: List[Tuple[str, List[Tuple[str, str]]]] = []
        for slot in segment.get("slots", []):
            slot_id = str(slot.get("id", "slot"))
            values = [str(value).strip() for value in slot.get("variants", []) if str(value).strip()]
            options = [(f"{slot_id}_{index + 1}", value) for index, value in enumerate(values)]
            fresh = [(key, value) for key, value in options if key not in recent] or options
            (required if slot.get("required", True) else optional).append((slot_id, fresh))
        if any(not options for _, options in required):
            return self._fallback(segment, target)

        combinations: List[Tuple[List[str], str, float]] = []
        required_values = product(*(options for _, options in required))
        for required_choice in required_values:
            base_ids = [key for key, _ in required_choice]
            base_text = "".join(value for _, value in required_choice)
            combinations.append((base_ids, base_text, _duration_score(base_text, target)))
            for slot_id, options in optional:
                for key, value in options[:5]:
                    ids = base_ids + [key]
                    text = base_text + value
                    combinations.append((ids, text, _duration_score(text, target)))

        combinations = [item for item in combinations if "+".join(item[0]) not in recent_combinations] or combinations
        combinations.sort(key=lambda item: item[2])
        if not combinations:
            return self._fallback(segment, target)
        shortlist = combinations[: min(5, len(combinations))]
        variant_ids, text, _ = self.random.choice(shortlist)
        combination_id = "+".join(variant_ids)
        return Selection(segment_id, "composable", variant_ids, combination_id, text, estimate_duration(text), target)

    def _fallback(self, segment: Dict[str, Any], target: float) -> Selection:
        text = str(segment.get("fallback_text") or segment.get("original_text") or "").strip()
        if not text:
            raise ValueError(f"{segment.get('id')}: no usable runtime candidate or fallback_text")
        return Selection(str(segment["id"]), str(segment.get("mode", "atomic")), ["fallback"], "fallback", text, estimate_duration(text), target, True)
