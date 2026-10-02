"""Cooperative job control and atomic progress/checkpoint files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time


class TaskInterrupted(Exception):
    pass


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class TaskControl:
    def __init__(self, directory: Path, on_progress=None):
        self.directory = directory
        self.checkpoint = directory / 'asr-checkpoint.json'
        self.on_progress = on_progress
        self.interrupted = threading.Event()

    def check(self):
        if self.interrupted.is_set():
            raise TaskInterrupted()

    def report(self):
        value = read_json(self.checkpoint)
        if value and self.on_progress:
            self.on_progress(value)

    def communicate(self, process, timeout=3600):
        deadline = time.monotonic() + timeout
        try:
            while True:
                self.report()
                self.check()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                try:
                    return process.communicate(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                try:
                    process.kill()
                    process.communicate(timeout=10)
                    if process.poll() is None:
                        raise OSError('worker still running')
                except (OSError, subprocess.TimeoutExpired) as exc:
                    from .qwen_asr_windows import WorkerLifecycleError
                    raise WorkerLifecycleError('ASR 子进程未能退出，请先停止软件再重试') from exc
            self.report()
            raise
