"""Ordered local WAV playback for the Windows client."""

from __future__ import annotations

from datetime import datetime, timezone
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, List, Optional


class PlaybackError(RuntimeError):
    pass


class PlaybackStopped(PlaybackError):
    pass


@dataclass
class PlaybackItem:
    item_id: str
    path: Path
    metadata_path: Path
    metadata: Dict[str, Any]
    sequence: float


class WinMMPlayer:
    """Use Windows' built-in MCI waveaudio driver; no external player window."""

    def __init__(self) -> None:
        self._mci = None
        self._error = None
        if os.name == "nt":
            self._mci = ctypes.WinDLL("winmm").mciSendStringW
            self._mci.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            self._mci.restype = ctypes.c_uint
            self._error = ctypes.WinDLL("winmm").mciGetErrorStringW
            self._error.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
            self._error.restype = ctypes.c_bool

    def play(
        self,
        path: Path,
        speed: float,
        volume: float,
        stop_event: threading.Event,
        pause_event: threading.Event,
    ) -> None:
        if self._mci is None:
            raise PlaybackError("Windows winmm playback is required")
        alias = f"ai_audio_{threading.get_ident()}"
        self._command(f'open "{str(path).replace(chr(34), "")}" type waveaudio alias {alias}')
        try:
            self._command(f"set {alias} time format milliseconds")
            self._command(f"set {alias} speed {max(1, round(speed * 1000))}")
            self._command(f"setaudio {alias} volume to {round(max(0.0, min(100.0, volume)) * 10)}")
            self._command(f"play {alias}")
            paused = False
            while True:
                if stop_event.is_set():
                    self._command(f"stop {alias}")
                    raise PlaybackStopped()
                if pause_event.is_set() and not paused:
                    self._command(f"pause {alias}")
                    paused = True
                elif not pause_event.is_set() and paused:
                    self._command(f"resume {alias}")
                    paused = False
                mode = self._status(alias)
                if mode in ("stopped", "not ready", ""):
                    if stop_event.is_set():
                        raise PlaybackStopped()
                    if mode == "not ready":
                        raise PlaybackError("Windows audio device is not ready")
                    return
                time.sleep(0.05)
        finally:
            self._command(f"close {alias}", ignore_error=True)

    def _status(self, alias: str) -> str:
        buffer = ctypes.create_unicode_buffer(64)
        self._command(f"status {alias} mode", buffer)
        return buffer.value.strip().lower()

    def _command(self, command: str, buffer: Any = None, ignore_error: bool = False) -> None:
        if self._mci is None:
            raise PlaybackError("Windows winmm playback is required")
        target = buffer if buffer is not None else ctypes.create_unicode_buffer(256)
        code = self._mci(command, target, len(target), None)
        if code and not ignore_error:
            message = ctypes.create_unicode_buffer(256)
            if self._error and self._error(code, message, len(message)):
                raise PlaybackError(message.value or f"MCI error {code}")
            raise PlaybackError(f"MCI error {code}")


class PlaybackController:
    """Play cached items in sequence order on a thread separate from downloads."""

    def __init__(
        self,
        cache_dir: Path,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        logger: Any = None,
        player: Optional[Any] = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.callback = callback
        self.logger = logger
        self.player = player or WinMMPlayer()
        self.speed = 1.0
        self.volume = 100.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.RLock()
        self._current: Optional[str] = None
        self._state = "stopped"
        self._error = ""
        self._recover_interrupted_items()

    def configure(self, speed: float = 1.0, volume: float = 100.0) -> None:
        speed = float(speed)
        volume = float(volume)
        if speed <= 0:
            raise ValueError("playback_speed must be positive")
        if not 0 <= volume <= 100:
            raise ValueError("playback_volume must be between 0 and 100")
        with self._lock:
            self.speed = speed
            self.volume = volume

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._stop.clear()
            self._pause.clear()
            self._error = ""
            self._state = "waiting"
            self._thread = threading.Thread(target=self._run, name="audio-playback", daemon=True)
            self._thread.start()
        self._emit()

    def pause(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive() and self._state in ("playing", "waiting"):
                self._pause.set()
                self._state = "paused"
        self._emit()

    def resume(self) -> None:
        with self._lock:
            self._pause.clear()
            if self._thread and self._thread.is_alive():
                self._state = "playing" if self._current else "waiting"
        self._emit()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._pause.clear()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        with self._lock:
            self._state = "stopped"
            self._current = None
        self._emit()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stats(self) -> Dict[str, Any]:
        items = self._items()
        with self._lock:
            state = self._state
            current = self._current
            error = self._error
        return {
            "playback_status": state,
            "playing": current or "-",
            "buffered_segments": sum(_playback_status(item.metadata) == "cached" for item in items),
            "played": sum(_playback_status(item.metadata) == "played" for item in items),
            "playback_failed": sum(_playback_status(item.metadata) == "playback_failed" for item in items),
            "cache": sum(item.path.is_file() for item in items),
            "playback_error": error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._next_item()
            if item is None:
                with self._lock:
                    if self._state != "paused":
                        self._state = "waiting"
                self._emit()
                self._stop.wait(0.2)
                continue

            self._set_status(item, "playing")
            with self._lock:
                self._current = item.item_id
                self._state = "paused" if self._pause.is_set() else "playing"
            self._emit()
            try:
                self.player.play(item.path, self.speed, self.volume, self._stop, self._pause)
            except PlaybackStopped:
                self._set_status(item, "cached")
                break
            except Exception as exc:
                self._set_status(item, "playback_failed", str(exc))
                with self._lock:
                    self._error = str(exc)
                if self.logger:
                    self.logger.error("playback failed %s: %s", item.item_id, exc)
            else:
                self._set_status(item, "played")
            finally:
                with self._lock:
                    self._current = None
                    if not self._stop.is_set():
                        self._state = "playing"
                self._emit()

        with self._lock:
            self._state = "stopped"
            self._current = None
        self._emit()

    def _next_item(self) -> Optional[PlaybackItem]:
        items = [item for item in self._items() if _playback_status(item.metadata) == "cached"]
        return min(items, key=lambda item: (item.sequence, item.item_id)) if items else None

    def _items(self) -> List[PlaybackItem]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for metadata_path in self.cache_dir.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            path = self.cache_dir / f"{metadata_path.stem}.wav"
            if not path.is_file():
                continue
            sequence = _sequence(metadata)
            items.append(PlaybackItem(metadata_path.stem, path, metadata_path, metadata, sequence))
        return items

    def _set_status(self, item: PlaybackItem, status: str, error: str = "") -> None:
        item.metadata["playback_status"] = status
        item.metadata["playback_updated_at"] = _utc_now()
        if error:
            item.metadata["playback_error"] = error
        elif status != "playback_failed":
            item.metadata.pop("playback_error", None)
        _atomic_write_json(item.metadata_path, item.metadata)

    def _recover_interrupted_items(self) -> None:
        for item in self._items():
            if _playback_status(item.metadata) in ("playing", "paused"):
                self._set_status(item, "cached")

    def _emit(self) -> None:
        if self.callback:
            self.callback(self.stats())


def _playback_status(metadata: Dict[str, Any]) -> str:
    value = metadata.get("playback_status")
    if value in ("cached", "playing", "paused", "played", "playback_failed"):
        return str(value) if metadata.get("status") == "completed" else "unknown"
    # Cache files created by the transport-only client predate this field.
    return "cached" if metadata.get("status") == "completed" else "unknown"


def _sequence(metadata: Dict[str, Any]) -> float:
    value = metadata.get("sequence")
    if value is None and isinstance(metadata.get("server_metadata"), dict):
        value = metadata["server_metadata"].get("sequence")
    try:
        return float(value)
    except (TypeError, ValueError):
        # Missing sequence is an invalid producer item; put it after sequenced audio.
        return float("inf")


def _atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
