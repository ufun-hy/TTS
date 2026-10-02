"""Stop a Windows runtime after its browser windows have closed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
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
    with (data / 'logs' / 'session.log').open('ab') as log:
        subprocess.Popen([str(python), str(root / 'local_runtime' / 'session.py'), str(root), str(data), str(stamp)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))


def guard(root: Path, data: Path, stamp: float):
    seen_view = False
    empty_since = None
    while True:
        time.sleep(3)
        try:
            if json.loads((data/'runtime/session.json').read_text())['started'] != stamp:
                return
        except (OSError, ValueError, KeyError):
            return
        now = time.time()
        active = False
        for path in (data/'runtime/views').glob('*.json'):
            try:
                value = json.loads(path.read_text())
                if value['seen'] >= stamp:
                    seen_view = True
                    active |= not value['closed'] and now-value['seen'] < 90
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if active:
            empty_since = None
            continue
        if not seen_view and now-stamp < 300:
            continue
        empty_since = empty_since or now
        if now-empty_since < 15:
            continue
        command = [sys.executable, str(root/'scripts/windows-runtime.py'), 'stop', '--data', str(data)]
        result = subprocess.run(command, timeout=180)
        print('Browser session ended; runtime stop exit='+str(result.returncode), flush=True)
        return


if __name__ == '__main__':
    guard(Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]))
