"""Durable replay task journal, not a source of automatic playback permission."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import uuid

REPLAY_ID = re.compile(r"^replay_[0-9a-f]{16}$")
AUDIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ReplayJournal:
    def __init__(self, root: Path):
        self.path = Path(root) / "replay-state.json"

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != 1:
            return {}
        sid, ids = data.get("session_id"), data.get("item_ids")
        status = data.get("status")
        if not isinstance(sid, str) or not REPLAY_ID.fullmatch(sid):
            return {}
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            return {}
        if not all(isinstance(x, str) and AUDIO_ID.fullmatch(x) for x in ids):
            return {}
        if status not in ("running", "paused", "stopped"):
            return {}
        return {"session_id": sid, "item_ids": ids, "items": len(ids), "status": status}

    def save(self, session_id: str, ids: list[str], status: str):
        value = {
            "schema": 1, "session_id": session_id,
            "item_ids": list(ids), "status": status,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("." + self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
