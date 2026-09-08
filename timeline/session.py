"""Persistent, per-run state for Timeline Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RuntimeSession:
    session_id: str
    timeline_id: str
    current_index: int = 0
    current_segment: str | None = None
    played_segments: List[Dict[str, Any]] = field(default_factory=list)
    recent_variants: Dict[str, List[str]] = field(default_factory=dict)
    recent_combinations: Dict[str, List[str]] = field(default_factory=dict)
    ready_audio: List[str] = field(default_factory=list)
    generating: List[str] = field(default_factory=list)
    statuses: Dict[str, str] = field(default_factory=dict)
    buffer_underrun_count: int = 0
    fallback_count: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def remember(self, segment_id: str, variant_ids: List[str], combination_id: str, window: int) -> None:
        variants = self.recent_variants.setdefault(segment_id, [])
        variants.extend(variant_ids)
        self.recent_variants[segment_id] = variants[-window:] if window else []
        combinations = self.recent_combinations.setdefault(segment_id, [])
        combinations.append(combination_id)
        self.recent_combinations[segment_id] = combinations[-window:] if window else []
        self.updated_at = utc_now()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: value for key, value in self.__dict__.items()}
        payload["updated_at"] = utc_now()
        self.updated_at = payload["updated_at"]
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
