"""Record UI presence without controlling background service lifetime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time


def view_event(data: Path, view: str, closed: bool):
    if not re.fullmatch(r'[a-zA-Z0-9-]{8,80}', view):
        raise ValueError('Invalid view identifier')
    path = data / 'runtime' / 'views' / (view+'.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'seen': time.time(), 'closed': closed}), encoding='utf-8')
    os.replace(temporary, path)


def launch_guardian(root: Path, data: Path, python: Path):
    stamp = time.time()
    marker = data / 'runtime' / 'session.json'
    marker.write_text(json.dumps({'started': stamp}), encoding='utf-8')
    # Invalidate any guardian from an older runtime; UI heartbeats no longer
    # own service lifetime. Only explicit runtime stop may terminate tasks.


def guard(root: Path, data: Path, stamp: float):
    """Compatibility entrypoint for older launchers; never stop background work."""
    return


if __name__ == '__main__':
    guard(Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]))
