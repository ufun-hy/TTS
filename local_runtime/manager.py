"""Process-local state machine and one-owner GPU lease.

This is intentionally a small coordination primitive.  The actual inference
workers remain separate processes; this object prevents two local entrypoints
from starting them at the same time and keeps failed releases visible.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import threading
import time
import uuid
from typing import Callable, Iterator, Optional

from .file_lease import FileGpuLease


class RuntimeStage(str, Enum):
    IDLE = "IDLE"
    ASR = "ASR"
    REWRITE = "REWRITE"
    TTS_PREPARING = "TTS_PREPARING"
    LIVE = "LIVE"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class RuntimeBusyError(RuntimeError):
    """Raised when another operation currently owns the local GPU lease."""

    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        owner = snapshot.get("gpu_owner") or "unknown"
        stage = snapshot.get("state") or "busy"
        super().__init__(f"local GPU is busy: {stage} ({owner})")


@dataclass(frozen=True)
class RuntimeLease:
    token: str
    owner: str
    stage: RuntimeStage


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeManager:
    """Own one local operation and expose a truthful, JSON-ready snapshot."""

    def __init__(self, release: Optional[Callable[[], bool]] = None, lock_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._release_callback = release
        self._lock_path = lock_path
        self._file_lease: FileGpuLease | None = None
        self._stage = RuntimeStage.IDLE
        self._owner = ""
        self._model = ""
        self._pid: Optional[int] = None
        self._started_at = ""
        self._last_error = ""
        self._model_released = True
        self._lease: Optional[RuntimeLease] = None
        self._stop_requested = False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            lease = self._lease
            return {
                "state": self._stage.value,
                "gpu_owner": self._owner,
                "operation": lease.token if lease else "",
                "active_model": self._model,
                "pid": self._pid,
                "started_at": self._started_at,
                "model_released": self._model_released,
                "stop_requested": self._stop_requested,
                "last_error": self._last_error,
            }

    def acquire(self, stage: RuntimeStage | str, owner: str, model: str = "", pid: int | None = None) -> RuntimeLease:
        stage = RuntimeStage(stage)
        if stage in (RuntimeStage.IDLE, RuntimeStage.STOPPING, RuntimeStage.ERROR):
            raise ValueError("an active lease must use ASR, REWRITE, TTS_PREPARING or LIVE")
        with self._lock:
            if self._lease is not None or self._stage not in (RuntimeStage.IDLE,):
                raise RuntimeBusyError(self.snapshot())
            if not owner.strip():
                raise ValueError("owner is required")
            lease = RuntimeLease(uuid.uuid4().hex[:12], owner.strip(), stage)
            if self._lock_path:
                file_lease = FileGpuLease(self._lock_path, owner.strip(), model.strip(), stage.value)
                if not file_lease.acquire():
                    current = FileGpuLease.read(self._lock_path)
                    snapshot = self.snapshot()
                    snapshot.update({"state": current.get("stage", "BUSY"), "gpu_owner": current.get("owner", "unknown"), "active_model": current.get("model", "")})
                    raise RuntimeBusyError(snapshot)
                self._file_lease = file_lease
            self._lease = lease
            self._stage = stage
            self._owner = lease.owner
            self._model = model.strip()
            self._pid = pid
            self._started_at = _timestamp()
            self._last_error = ""
            self._model_released = False
            self._stop_requested = False
            return lease

    def set_pid(self, lease: RuntimeLease, pid: int | None) -> None:
        with self._lock:
            self._require(lease)
            self._pid = pid

    def set_stage(self, lease: RuntimeLease, stage: RuntimeStage | str) -> None:
        stage = RuntimeStage(stage)
        if stage in (RuntimeStage.IDLE, RuntimeStage.ERROR):
            raise ValueError("use release() to leave an active lease")
        with self._lock:
            self._require(lease)
            self._stage = stage
            self._stop_requested = stage == RuntimeStage.STOPPING

    def request_stop(self, lease: RuntimeLease) -> None:
        with self._lock:
            self._require(lease)
            self._stage = RuntimeStage.STOPPING
            self._stop_requested = True

    def release(self, lease: RuntimeLease, error: str = "") -> bool:
        """Release only after the worker callback confirms termination.

        A false callback result keeps the lease and enters ERROR.  This is the
        important safety property: a failed terminate cannot be mistaken for a
        free GPU.
        """
        with self._lock:
            self._require(lease)
            if error:
                self._stage = RuntimeStage.ERROR
                self._last_error = error
                self._model_released = False
                return False
            released = True if self._release_callback is None else bool(self._release_callback())
            if released and self._file_lease is not None:
                released = self._file_lease.release()
            if not released:
                self._stage = RuntimeStage.ERROR
                self._last_error = "worker did not confirm model/process release"
                self._model_released = False
                return False
            self._stage = RuntimeStage.IDLE
            self._owner = ""
            self._model = ""
            self._pid = None
            self._started_at = ""
            self._model_released = True
            self._stop_requested = False
            self._lease = None
            self._file_lease = None
            return True

    def clear_error(self) -> bool:
        """Clear an error only when no worker remains and release is confirmed."""
        with self._lock:
            if self._stage != RuntimeStage.ERROR or not self._model_released or self._lease is not None:
                return False
            self._stage = RuntimeStage.IDLE
            self._last_error = ""
            return True

    @contextmanager
    def operation(self, stage: RuntimeStage | str, owner: str, model: str = "", pid: int | None = None) -> Iterator[RuntimeLease]:
        lease = self.acquire(stage, owner, model, pid)
        try:
            yield lease
        except Exception as exc:
            self.release(lease, str(exc))
            raise
        else:
            self.release(lease)

    def _require(self, lease: RuntimeLease) -> None:
        if self._lease != lease:
            raise RuntimeError("runtime lease is no longer active")
