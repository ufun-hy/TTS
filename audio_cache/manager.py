"""Filesystem-backed ordered audio cache with atomic state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
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
    """Keep one item per directory so a state move is a single filesystem rename."""

    def __init__(self, root: Path, processor: Optional[Callable[[bytes, Dict[str, Any]], Any]] = None) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._processor = processor or AudioProcessor().process
        for state in STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True)

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
            return AudioItem(item_id, "pending", destination, payload)

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
            processing = self.root / "processing" / item.id
            os.replace(item.directory, processing)
            item = self._read_item(processing, "processing")
            self._update_status(item, "processing")
        try:
            if not item.audio_path.is_file():
                raise AudioCacheError("pending item has no audio.wav")
            result = self._processor(item.audio_path.read_bytes(), item.metadata)
            processed_audio, processing_metadata = _processing_payload(result, item.metadata)
            temporary = item.directory / ".audio.wav.tmp"
            temporary.write_bytes(processed_audio)
            os.replace(temporary, item.audio_path)
            item.metadata.update(processing_metadata)
            item.metadata["status"] = "ready"
            self._write_metadata(item.directory, item.metadata)
            ready = self.root / "ready" / item.id
            with self._lock:
                os.replace(item.directory, ready)
                return self._read_item(ready, "ready")
        except Exception as exc:
            self._mark_failed(item.id, str(exc), current_state="processing")
            raise

    def claim_next(self) -> Optional[AudioItem]:
        with self._lock:
            items = sorted(self.list_items("ready"), key=_sort_key)
            if not items:
                return None
            item = items[0]
            processing = self.root / "processing" / item.id
            os.replace(item.directory, processing)
            item = self._read_item(processing, "processing")
            self._update_status(item, "processing")
            return item

    def ack(self, item_id: str, status: str = "completed") -> AudioItem:
        if status not in ("completed", "failed"):
            raise AudioCacheError("ack status must be completed or failed")
        with self._lock:
            current = self._find(item_id, ("processing",))
            if current is None:
                current = self._find(item_id, (status,))
                if current is not None:
                    return current
                other = self.get(item_id)
                if other:
                    raise AudioCacheError(f"cannot ack {item_id} from {other.status}")
                raise AudioCacheError(f"audio not found: {item_id}")
            self._update_status(current, status)
            destination = self.root / status / item_id
            os.replace(current.directory, destination)
            return self._read_item(destination, status)

    def get(self, item_id: str) -> Optional[AudioItem]:
        item_id = self._validate_id(item_id)
        with self._lock:
            for state in STATES:
                item = self._find(item_id, (state,))
                if item:
                    return item
        return None

    def has(self, item_id: str) -> bool:
        return self.get(item_id) is not None

    def audio_path(self, item_id: str) -> Optional[Path]:
        item = self.get(item_id)
        if not item or not item.audio_path.is_file():
            return None
        return item.audio_path

    def list_items(self, state: Optional[str] = None) -> List[AudioItem]:
        states = (state,) if state else STATES
        if any(value not in STATES for value in states):
            raise AudioCacheError(f"invalid state: {state}")
        with self._lock:
            items: List[AudioItem] = []
            for value in states:
                directory = self.root / value
                for child in directory.iterdir():
                    if child.is_dir() and ITEM_ID.fullmatch(child.name):
                        try:
                            items.append(self._read_item(child, value))
                        except (OSError, ValueError, json.JSONDecodeError):
                            continue
            return items

    def stats(self) -> Dict[str, int]:
        return {state: len(self.list_items(state)) for state in STATES}

    def _find(self, item_id: str, states: Iterable[str]) -> Optional[AudioItem]:
        item_id = self._validate_id(item_id)
        for state in states:
            directory = self.root / state / item_id
            if directory.is_dir():
                return self._read_item(directory, state)
        return None

    def _read_item(self, directory: Path, state: str) -> AudioItem:
        with (directory / "metadata.json").open(encoding="utf-8") as handle:
            metadata = json.load(handle)
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid metadata: {directory}")
        return AudioItem(str(metadata.get("id") or directory.name), state, directory, metadata)

    def _update_status(self, item: AudioItem, status: str) -> None:
        item.status = status
        item.metadata["status"] = status
        item.metadata["updated_at"] = _utc_now()
        self._write_metadata(item.directory, item.metadata)

    def _mark_failed(self, item_id: str, error: str, current_state: str = "pending") -> None:
        with self._lock:
            item = self._find(item_id, (current_state,))
            if not item:
                return
            item.metadata["error"] = error
            self._update_status(item, "failed")
            destination = self.root / "failed" / item_id
            os.replace(item.directory, destination)

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
