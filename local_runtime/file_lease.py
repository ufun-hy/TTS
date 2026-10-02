"""Crash-tolerant single-owner marker shared by the local worker processes."""

from __future__ import annotations

import json
import ctypes
import os
from pathlib import Path
import time
import uuid
from typing import Any


class FileGpuLease:
    """Acquire an atomic marker; recover only markers whose PID is gone."""

    def __init__(self, path: Path, owner: str = "", model: str = "", stage: str = "") -> None:
        self.path = Path(path)
        self.owner = owner
        self.model = model
        self.stage = stage
        self.token = uuid.uuid4().hex
        self.acquired = False

    @staticmethod
    def read(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"token": self.token, "pid": os.getpid(), "owner": self.owner, "model": self.model, "stage": self.stage, "started_at": time.time()}
        if os.name == 'nt':
            payload['process_created'] = _windows_process_created(os.getpid())
        for _ in range(2):
            try:
                descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                current = self.read(self.path)
                pid = current.get("pid")
                created = _windows_process_created(pid) if os.name == 'nt' and isinstance(pid, int) else None
                reused = bool(created and current.get('process_created') and created != current['process_created'])
                if isinstance(pid, int) and pid != os.getpid() and (not _pid_alive(pid) or reused):
                    try:
                        self.path.unlink()
                    except OSError:
                        return False
                    continue
                return False
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            self.acquired = True
            return True
        return False

    def release(self) -> bool:
        if not self.acquired:
            return True
        current = self.read(self.path)
        if current.get("token") != self.token:
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        self.acquired = False
        return True


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Access denied/unknown is not proof that an owner has exited.
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Query a process without ever signalling it; unknown means still owned."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: PID gone
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: exited
    finally:
        kernel.CloseHandle(handle)


def _windows_process_created(pid: int):
    """Process creation FILETIME distinguishes recycled process identifiers."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
        return None
    finally:
        kernel.CloseHandle(handle)
