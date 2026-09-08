"""Natural duration windows for live speech candidates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DurationWindow:
    target: float
    preferred_low: float
    preferred_high: float
    hard_low: float
    hard_high: float

    def status(self, duration: float) -> str:
        if duration < self.target * 0.4:
            return "extreme_too_short"
        if duration > self.target * 2.0:
            return "extreme_too_long"
        if self.preferred_low <= duration <= self.preferred_high:
            return "preferred"
        return "acceptable"


class DurationPolicy:
    version = "duration-guard-v1"

    def window(self, target: float) -> DurationWindow:
        target = max(0.01, float(target))
        if target < 4:
            preferred_low, preferred_high = target * 0.70, target * 1.40
            hard_low, hard_high = target * 0.50, target * 1.80
        elif target < 8:
            preferred_low, preferred_high = target * 0.75, target * 1.30
            hard_low, hard_high = target * 0.60, target * 1.55
        elif target <= 15:
            preferred_low, preferred_high = target * 0.80, target * 1.25
            hard_low, hard_high = target * 0.65, target * 1.40
        else:
            preferred_low, preferred_high = target * 0.85, target * 1.20
            hard_low, hard_high = target * 0.75, target * 1.35
        return DurationWindow(target, preferred_low, preferred_high, hard_low, hard_high)
