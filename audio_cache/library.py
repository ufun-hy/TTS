"""Immutable local WAV inventory separate from delivery/ACK cache states."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class AudioLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def store(self, item_id: str, audio_path: Path, metadata: dict) -> dict:
        if not ID.fullmatch(item_id):
            raise ValueError("invalid library item id")
        if not audio_path.is_file():
            raise FileNotFoundError(audio_path)
        with self._lock:
            target = self.root / item_id
            if target.exists():
                # Immutable entries cannot silently be overwritten.
                raise ValueError("library item already exists")
            temporary = self.root / ("." + item_id + "." + uuid.uuid4().hex + ".tmp")
            temporary.mkdir()
            try:
                shutil.copyfile(audio_path, temporary / "audio.wav")
                if not (temporary / "audio.wav").stat().st_size:
                    raise ValueError("empty audio file")
                info = {k: metadata.get(k) for k in (
                    "session_id", "sequence", "text", "voice", "duration",
                    "created_at", "playback_speed", "volume", "source"
                )}
                info["id"] = item_id
                info["size_bytes"] = (temporary / "audio.wav").stat().st_size
                (temporary / "metadata.json").write_text(
                    json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                os.replace(temporary, target)
                return info
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    def list(self, session_id: str = "", limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        results = []
        with self._lock:
            for directory in self.root.iterdir():
                if not directory.is_dir() or not ID.fullmatch(directory.name):
                    continue
                try:
                    info = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
                    if not isinstance(info, dict) or info.get("id") != directory.name:
                        continue
                    if not (directory / "audio.wav").is_file():
                        continue
                    if session_id and info.get("session_id") != session_id:
                        continue
                    results.append(info)
                except (OSError, ValueError, TypeError):
                    continue
        results.sort(key=lambda x: (str(x.get("created_at") or ""), str(x.get("id"))), reverse=True)
        return results[:limit]

    def get(self, item_id: str) -> dict | None:
        if not ID.fullmatch(item_id):
            raise ValueError("invalid library item id")
        with self._lock:
            meta = self.root / item_id / "metadata.json"
            audio = self.root / item_id / "audio.wav"
            if not meta.is_file() or not audio.is_file():
                return None
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("id") == item_id:
                    return payload
            except (OSError, ValueError):
                pass
            return None

    def file(self, item_id: str) -> Path | None:
        if not ID.fullmatch(item_id):
            raise ValueError("invalid library item id")
        path = self.root / item_id / "audio.wav"
        return path if path.is_file() else None
