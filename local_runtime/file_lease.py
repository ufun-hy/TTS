"""Crash-tolerant single-owner marker shared by the local worker processes."""

from __future__ import annotations

import json
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
        for _ in range(2):
            try:
                descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                current = self.read(self.path)
                pid = current.get("pid")
                if isinstance(pid, int) and pid != os.getpid() and not _pid_alive(pid):
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
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True
