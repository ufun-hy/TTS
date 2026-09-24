"""Incremental metadata reads for the local playback queue."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any


class PlaybackMetadataCache:
    """Re-enumerate names, but reopen only metadata replaced by a writer.

    Both download and playback writers atomically replace JSON files. Include
    file identity as well as size/mtime so same-size replacements are noticed.
    Returned dictionaries belong to the caller, never to this cache.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._entries: dict[str, tuple[tuple[int, ...], dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def invalidate(self, path: Path) -> None:
        # FAT volumes can give successive writes identical coarse timestamps.
        with self._lock:
            self._entries.pop(path.name, None)

    def read(self) -> list[tuple[Path, dict[str, Any]]]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with os.scandir(self.directory) as scan:
                entries = {entry.name: entry for entry in scan if entry.is_file()}
            names = {os.path.normcase(name) for name in entries}
            result = []
            retained = {}
            for name, entry in entries.items():
                if not os.path.normcase(name).endswith(".json") or os.path.normcase(name[:-5] + ".wav") not in names:
                    continue
                try:
                    stat = entry.stat()
                    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                    cached = self._entries.get(name)
                    if cached is not None and cached[0] == signature:
                        metadata = cached[1]
                    else:
                        metadata = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                        if not isinstance(metadata, dict):
                            continue
                    retained[name] = (signature, metadata)
                    # Only top-level playback fields are changed by the controller.
                    result.append((Path(entry.path), dict(metadata)))
                except (OSError, ValueError):
                    continue
            self._entries = retained
            return result
