"""Filesystem-backed ordered audio cache with in-memory state indexes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from .processing import AudioProcessor, ProcessingResult


STATES = ("pending", "ready", "processing", "completed", "failed")
ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class AudioCacheError(RuntimeError):
    pass


@dataclass
class AudioItem:
    id: str
    status: str
    directory: Path
    metadata: Dict[str, Any]

    @property
    def audio_path(self) -> Path:
        return self.directory / "audio.wav"

    @property
    def duration(self) -> float:
        try:
            return float(self.metadata.get("duration", self.metadata.get("raw_duration", 0)))
        except (TypeError, ValueError):
            return 0.0

    def public_metadata(self) -> Dict[str, Any]:
        return dict(self.metadata)


class AudioCacheManager:
    """Keep filesystem durability while serving hot-path state from memory."""

    def __init__(self, root: Path, processor: Optional[Callable[[bytes, Dict[str, Any]], Any]] = None) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._processor = processor or AudioProcessor().process
        self._items: Dict[str, AudioItem] = {}
        self._state_counts: Dict[str, int] = {state: 0 for state in STATES}
        self._session_counts: Dict[str, Dict[str, int]] = {}
        for state in STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True)
        self._load_index()

    def create_pending(self, metadata: Dict[str, Any], item_id: Optional[str] = None) -> AudioItem:
        with self._lock:
            item_id = self._validate_id(item_id or self._new_id())
            if self.has(item_id):
                raise AudioCacheError(f"audio id already exists: {item_id}")
            payload = dict(metadata)
            payload.update({"id": item_id, "status": "pending", "created_at": payload.get("created_at") or _utc_now()})
            temporary = self.root / "pending" / f".{item_id}.{uuid.uuid4().hex}.tmp"
            temporary.mkdir(parents=True)
            self._write_metadata(temporary, payload)
            destination = self.root / "pending" / item_id
            os.replace(temporary, destination)
            item = AudioItem(item_id, "pending", destination, payload)
            self._register(item)
            return item

    def add_audio(self, audio: bytes, metadata: Dict[str, Any], item_id: Optional[str] = None) -> AudioItem:
        item = self.create_pending(metadata, item_id)
        try:
            temporary = item.directory / ".audio.wav.tmp"
            temporary.write_bytes(audio)
            os.replace(temporary, item.audio_path)
            return self.process_pending(item.id)
        except Exception as exc:
            self._mark_failed(item.id, str(exc))
            raise

    def complete_pending(self, item_id: str, audio: bytes) -> AudioItem:
        with self._lock:
            item = self._find(item_id, ("pending",))
            if not item:
                raise AudioCacheError(f"pending audio not found: {item_id}")
            temporary = item.directory / ".audio.wav.tmp"
            temporary.write_bytes(audio)
            os.replace(temporary, item.audio_path)
        return self.process_pending(item_id)

    def process_pending(self, item_id: str) -> AudioItem:
        with self._lock:
            item = self._find(item_id, ("pending",))
            if not item:
                existing = self.get(item_id)
                if existing:
                    return existing
                raise AudioCacheError(f"pending audio not found: {item_id}")
            self._move_state(item, "processing")
        try:
            if not item.audio_path.is_file():
                raise AudioCacheError("pending item has no audio.wav")
            result = self._processor(item.audio_path.read_bytes(), item.metadata)
            processed_audio, processing_metadata = _processing_payload(result, item.metadata)
            temporary = item.directory / ".audio.wav.tmp"
            temporary.write_bytes(processed_audio)
            os.replace(temporary, item.audio_path)
            with self._lock:
                item.metadata.update(processing_metadata)
                return self._move_state(item, "ready")
        except Exception as exc:
            self._mark_failed(item.id, str(exc), current_state="processing")
            raise

    def claim_next(self) -> Optional[AudioItem]:
        with self._lock:
            items = sorted(
                (item for item in self._items.values() if item.status == "ready"),
                key=_sort_key,
            )
            if not items:
                return None
            return self._move_state(items[0], "processing")

    def ack(self, item_id: str, status: str = "completed") -> AudioItem:
        if status not in ("completed", "failed"):
            raise AudioCacheError("ack status must be completed or failed")
        with self._lock:
            current = self.get(item_id)
            if current is None:
                raise AudioCacheError(f"audio not found: {item_id}")
            if current.status == status:
                return current
            if current.status != "processing":
                raise AudioCacheError(f"cannot ack {item_id} from {current.status}")
            current = self._move_state(current, status)
            self._release_audio(current)
            return current

    def get(self, item_id: str) -> Optional[AudioItem]:
        item_id = self._validate_id(item_id)
        with self._lock:
            return self._items.get(item_id)

    def has(self, item_id: str) -> bool:
        return self.get(item_id) is not None

    def audio_path(self, item_id: str) -> Optional[Path]:
        item = self.get(item_id)
        if not item or not item.audio_path.is_file():
            return None
        return item.audio_path

    def list_items(self, state: Optional[str] = None) -> List[AudioItem]:
        if state is not None and state not in STATES:
            raise AudioCacheError(f"invalid state: {state}")
        with self._lock:
            if state is None:
                return list(self._items.values())
            return [item for item in self._items.values() if item.status == state]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._state_counts)

    def session_stats(self, session_id: str) -> Dict[str, int]:
        if not session_id:
            raise AudioCacheError("session_id is required")
        with self._lock:
            counts = self._session_counts.get(session_id)
            return dict(counts) if counts else {state: 0 for state in STATES}

    def cleanup_session(self, session_id: str) -> int:
        """Remove only cache items tagged with one live session."""
        if not session_id:
            raise AudioCacheError("session_id is required")
        removed = 0
        with self._lock:
            items = [item for item in self._items.values() if item.metadata.get("session_id") == session_id]
            for item in items:
                try:
                    shutil.rmtree(item.directory)
                except FileNotFoundError:
                    pass
                self._unregister(item)
                removed += 1
        return removed

    def _load_index(self) -> None:
        """Scan durable cache once at startup; hot paths use memory afterwards."""
        with self._lock:
            for state in STATES:
                directory = self.root / state
                for child in directory.iterdir():
                    if not child.is_dir() or not ITEM_ID.fullmatch(child.name):
                        continue
                    try:
                        item = self._read_item(child, state)
                    except (OSError, ValueError, json.JSONDecodeError):
                        continue
                    existing = self._items.get(item.id)
                    if existing is not None:
                        # Keep the most recently updated durable copy if an old
                        # interrupted run left a duplicate directory behind.
                        if str(existing.metadata.get("updated_at", existing.metadata.get("created_at", ""))) >= str(
                            item.metadata.get("updated_at", item.metadata.get("created_at", ""))
                        ):
                            continue
                        self._unregister(existing)
                    self._register(item)

    def _register(self, item: AudioItem) -> None:
        self._items[item.id] = item
        self._state_counts[item.status] += 1
        session_id = item.metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            counts = self._session_counts.setdefault(session_id, {state: 0 for state in STATES})
            counts[item.status] += 1

    def _unregister(self, item: AudioItem) -> None:
        current = self._items.get(item.id)
        if current is None:
            return
        self._state_counts[current.status] = max(0, self._state_counts[current.status] - 1)
        session_id = current.metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            counts = self._session_counts.get(session_id)
            if counts:
                counts[current.status] = max(0, counts[current.status] - 1)
                if not any(counts.values()):
                    self._session_counts.pop(session_id, None)
        self._items.pop(item.id, None)

    def _move_state(self, item: AudioItem, status: str) -> AudioItem:
        if status not in STATES:
            raise AudioCacheError(f"invalid state: {status}")
        old_status = item.status
        if old_status == status:
            return item
        destination = self.root / status / item.id
        item.metadata["status"] = status
        item.metadata["updated_at"] = _utc_now()
        self._write_metadata(item.directory, item.metadata)
        os.replace(item.directory, destination)

        self._state_counts[old_status] = max(0, self._state_counts[old_status] - 1)
        self._state_counts[status] += 1
        session_id = item.metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            counts = self._session_counts.setdefault(session_id, {state: 0 for state in STATES})
            counts[old_status] = max(0, counts[old_status] - 1)
            counts[status] += 1

        item.status = status
        item.directory = destination
        return item

    def _release_audio(self, item: AudioItem) -> None:
        """Drop the Mac WAV after Windows has durably acknowledged receipt."""
        try:
            size = item.audio_path.stat().st_size
        except FileNotFoundError:
            return
        try:
            item.audio_path.unlink()
        except FileNotFoundError:
            return
        item.metadata["audio_released"] = True
        item.metadata["audio_released_bytes"] = size
        item.metadata["audio_released_at"] = _utc_now()
        self._write_metadata(item.directory, item.metadata)

    def _find(self, item_id: str, states: Iterable[str]) -> Optional[AudioItem]:
        item_id = self._validate_id(item_id)
        item = self._items.get(item_id)
        return item if item and item.status in set(states) else None

    def _read_item(self, directory: Path, state: str) -> AudioItem:
        with (directory / "metadata.json").open(encoding="utf-8") as handle:
            metadata = json.load(handle)
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid metadata: {directory}")
        metadata["status"] = state
        return AudioItem(str(metadata.get("id") or directory.name), state, directory, metadata)

    def _mark_failed(self, item_id: str, error: str, current_state: str = "pending") -> None:
        with self._lock:
            item = self._find(item_id, (current_state,))
            if not item:
                return
            item.metadata["error"] = error
            self._move_state(item, "failed")

    @staticmethod
    def _write_metadata(directory: Path, payload: Dict[str, Any]) -> None:
        temporary = directory / ".metadata.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, directory / "metadata.json")

    @staticmethod
    def _validate_id(item_id: str) -> str:
        if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
            raise AudioCacheError("id must contain only letters, numbers, _, -, or .")
        return item_id

    @staticmethod
    def _new_id() -> str:
        return f"audio_{uuid.uuid4().hex[:16]}"


def _processing_payload(result: Any, original: Dict[str, Any]) -> tuple[bytes, Dict[str, Any]]:
    if isinstance(result, ProcessingResult):
        return result.audio, {
            "raw_duration": result.raw_duration,
            "duration": result.duration,
            "speed_factor": result.speed_factor,
            "volume_gain": result.volume_gain,
            "session_volume": result.session_volume,
            "session_volume_gain": result.session_volume_gain,
            "processing_warnings": result.warnings,
        }
    if isinstance(result, bytes):
        return result, {"duration": original.get("duration", 0)}
    raise AudioCacheError("audio processor returned an unsupported result")


def _sort_key(item: AudioItem) -> tuple[Any, str, str]:
    sequence = item.metadata.get("sequence")
    try:
        order = (0, float(sequence)) if sequence is not None else (1, 0.0)
    except (TypeError, ValueError):
        order = (1, 0.0)
    return order, str(item.metadata.get("created_at", "")), item.id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
