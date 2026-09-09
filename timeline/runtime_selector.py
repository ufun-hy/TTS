"""Runtime selection for model-generated candidates."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import random
from typing import Any, Dict, List, Tuple

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


class RuntimeSelector:
    def __init__(self, cooldown_window: int = 3, tolerance: float = 0.15, seed: int | None = None) -> None:
        self.cooldown_window = cooldown_window
        self.tolerance = tolerance
        self.random = random.Random(seed)

    def select(self, segment: Dict[str, Any], session: RuntimeSession) -> Selection:
        segment_id = str(segment["id"])
        target = float(segment.get("speech_duration", segment.get("duration_target", float(segment["end"]) - float(segment["start"]))))
        candidates = [(f"candidate_{i + 1:02d}", str(v).strip()) for i, v in enumerate(segment.get("candidates", [])) if str(v).strip()]
        if candidates:
            recent = set(session.recent_variants.get(segment_id, []))
            available = [item for item in candidates if item[0] not in recent] or candidates
            variant_id, text = self.random.choice(available)
            result = Selection(segment_id, "direct_model", [variant_id], variant_id, text, estimate_duration(text), target)
            session.remember(segment_id, result.variant_ids, result.combination_id, self.cooldown_window)
            return result
        mode = str(segment.get("mode", "atomic"))
        result = self._select_legacy(segment, session, target, mode)
        session.remember(segment_id, result.variant_ids, result.combination_id, self.cooldown_window)
        return result

    def _select_legacy(self, segment: Dict[str, Any], session: RuntimeSession, target: float, mode: str) -> Selection:
        segment_id = str(segment["id"])
        if mode in {"composable", "compact_composable"}:
            required: List[Tuple[str, List[Tuple[str, str]]]] = []
            optional: List[Tuple[str, List[Tuple[str, str]]]] = []
            recent = set(session.recent_variants.get(segment_id, []))
            for slot in segment.get("slots", []):
                slot_id = str(slot.get("id", "slot"))
                values = [str(value).strip() for value in slot.get("variants", []) if str(value).strip()]
                options = [(f"{slot_id}_{index + 1}", value) for index, value in enumerate(values)]
                fresh = [item for item in options if item[0] not in recent] or options
                (required if slot.get("required", True) else optional).append((slot_id, fresh))
            if any(not values for _, values in required):
                return self._fallback(segment, target)
            combinations = []
            for chosen in product(*(values for _, values in required)):
                ids = [key for key, _ in chosen]
                text = "".join(value for _, value in chosen)
                combinations.append((ids, text))
                for _, values in optional:
                    if values:
                        key, value = self.random.choice(values)
                        combinations.append((ids + [key], text + value))
            if combinations:
                ids, text = self.random.choice(combinations)
                combo = "+".join(ids)
                return Selection(segment_id, "composable", ids, combo, text, estimate_duration(text), target)
            return self._fallback(segment, target)
        values = [str(value).strip() for value in segment.get("variants", []) if str(value).strip()]
        options = [(f"variant_{index + 1}", value) for index, value in enumerate(values)]
        recent = set(session.recent_variants.get(segment_id, []))
        available = [item for item in options if item[0] not in recent] or options
        if not available:
            return self._fallback(segment, target)
        variant_id, text = self.random.choice(available)
        return Selection(segment_id, "atomic", [variant_id], variant_id, text, estimate_duration(text), target)

    def _fallback(self, segment: Dict[str, Any], target: float) -> Selection:
        text = str(segment.get("fallback_text") or segment.get("original_text") or "").strip()
        if not text:
            raise ValueError(f"{segment.get('id')}: no usable runtime candidate or fallback_text")
        return Selection(str(segment["id"]), "fallback", ["fallback"], "fallback", text, estimate_duration(text), target, True)
