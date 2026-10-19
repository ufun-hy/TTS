"""Immutable local WAV inventory separate from delivery/ACK cache states."""
from __future__ import annotations

import json
import shutil
import os
from pathlib import Path
import re
import threading
import uuid

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class AudioLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def capacity(self) -> dict:
        """Exact disk usage of archived assets, including metadata."""
        with self._lock:
            count = 0
            used = 0
            for entry in self.root.iterdir():
                if not entry.is_dir() or not ID.fullmatch(entry.name):
                    continue
                audio = entry / "audio.wav"
                meta = entry / "metadata.json"
                if not audio.is_file() or not meta.is_file():
                    continue
                count += 1
                used += audio.stat().st_size + meta.stat().st_size
            try:
                cap_gib = float(os.environ.get("AI_LIVE_STUDIO_LIBRARY_MAX_GIB", "20"))
            except ValueError as exc:
                raise ValueError("invalid AI_LIVE_STUDIO_LIBRARY_MAX_GIB") from exc
            if not 0.01 <= cap_gib <= 100000:
                raise ValueError("library capacity must be between 0.01 and 100000 GiB")
            max_bytes = int(cap_gib * (1024 ** 3))
            return {
                "item_count": count, "used_bytes": used,
                "limit_bytes": max_bytes, "remaining_bytes": max(0, max_bytes - used),
            }

    def delete(self, item_ids: list[str], protected_ids: set[str]) -> dict:
        """Explicit archive deletion only; reject the entire request on conflict."""
        if not isinstance(item_ids, list) or not item_ids or len(item_ids) > 100:
            raise ValueError("item_ids must contain 1 to 100 ids")
        if any(not isinstance(x, str) or not ID.fullmatch(x) for x in item_ids):
            raise ValueError("invalid inventory id")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("duplicate inventory ids")
        with self._lock:
            if any(x in protected_ids for x in item_ids):
                raise ValueError("inventory is referenced by an active playback task")
            if any(self.get(x) is None for x in item_ids):
                raise ValueError("one or more inventory items do not exist")
            for item_id in item_ids:
                shutil.rmtree(self.root / item_id)
            return {"deleted": len(item_ids), "capacity": self.capacity()}

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
            usage = self.capacity()
            size = audio_path.stat().st_size
            # Reserve extra room for metadata before starting a durable copy.
            if usage["used_bytes"] + size + 16384 > usage["limit_bytes"]:
                raise ValueError("audio library capacity reached; delete old inventory explicitly")
            free_space = shutil.disk_usage(self.root).free
            if free_space < size + 16 * 1024 * 1024:
                raise OSError("not enough disk space for archived audio")
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
