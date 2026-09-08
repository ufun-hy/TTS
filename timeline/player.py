"""Sequential audio playback; no selection or generation logic belongs here."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time
from typing import Tuple


class Player:
    def __init__(self, executable: str = "/usr/bin/afplay", dry_run: bool = False) -> None:
        self.executable = executable
        self.dry_run = dry_run

    def play(self, path: Path | None) -> Tuple[float, float]:
        started = time.monotonic()
        if not self.dry_run:
            if path is None or not path.is_file():
                raise FileNotFoundError("audio file is not ready")
            subprocess.run([self.executable, str(path)], check=True)
        return started, time.monotonic()
