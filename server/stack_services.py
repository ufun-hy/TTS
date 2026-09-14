"""Manual launchd lifecycle for the three auxiliary TTS services (macOS)."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import fcntl
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Service:
    name: str
    title: str
    port: int
    health: str
    markers: tuple[str, ...]
    label: str


SERVICES = (
    Service('audio-cache', 'Audio Cache', 8000, '/health', ('scripts/audio-cache-server.py',), 'com.ufun.tts.audio-cache'),
    Service('text-studio', 'Text Studio', 8770, '/api/health', ('server/text_studio_entry.py', 'server/text_studio.py'), 'com.ufun.tts.text-studio'),
    Service('recording-transcript', 'Transcript', 8771, '/api/health', ('server/recording_transcript.py',), 'com.ufun.tts.recording-transcript'),
)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=20)


def target(service: Service) -> str:
    return f'gui/{os.getuid()}/{service.label}'


def plist_path(root: Path, service: Service) -> Path:
    return root / 'runtime' / 'launchd' / f'{service.name}.plist'


def job_info(service: Service) -> dict:
    result = run('/bin/launchctl', 'print', target(service))
    if result.returncode:
        return {}
    # Do not echo the whole launchctl dump: it can contain inherited environment.
    fields = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r'\s*(path|state|pid|last exit code|last terminating signal) = (.+)', line)
        if match and match[1] not in fields:
            fields[match[1]] = match[2].strip()
    fields['loaded'] = True
    return fields


def owns_job(root: Path, service: Service, info: dict) -> bool:
    return bool(info and Path(info.get('path', '')).resolve() == plist_path(root, service).resolve())


def listener_pids(port: int) -> list[int]:
    result = run('/usr/sbin/lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t')
    return sorted({int(pid) for pid in result.stdout.split() if pid.isdigit()})


def owns_process(root: Path, service: Service, pid: int) -> bool:
    command = run('/bin/ps', '-p', str(pid), '-o', 'command=').stdout.strip()
    # Match complete script paths, never a bare filename or a substring suffix.
    return any(re.search(r'(?:^|\s)' + re.escape(str(root / marker)) + r'(?:\s|$)', command)
               for marker in service.markers)


def health_ok(service: Service) -> bool:
    try:
        with urlopen(f'http://127.0.0.1:{service.port}{service.health}', timeout=2) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def ready(root: Path, service: Service, info: dict | None = None) -> bool:
    info = job_info(service) if info is None else info
    pid = int(info.get('pid', '0'))
    return (owns_job(root, service, info) and pid > 0 and owns_process(root, service, pid)
            and listener_pids(service.port) == [pid] and health_ok(service))


def status(root: Path, service: Service) -> bool:
    info = job_info(service)
    healthy = health_ok(service)
    listeners = listener_pids(service.port)
    ok = ready(root, service, info)
    state = info.get('state', 'unloaded') if not info or owns_job(root, service, info) else 'foreign-job'
    last_exit = info.get('last terminating signal', info.get('last exit code', 'unknown'))
    print(f'{service.title}: {"READY" if ok else "DOWN"} launchd={state} '
          f'PID={info.get("pid", "-")} last-exit={last_exit} port={service.port} '
          f'listeners={",".join(map(str, listeners)) or "-"} health={"OK" if healthy else "DOWN"}', flush=True)
    return ok


def preflight(root: Path) -> None:
    """Validate persistent credentials before a restart can stop anything."""
    if sys.platform != 'darwin':
        raise RuntimeError('Managed stack services require macOS launchd.')
    if run('/bin/launchctl', 'print', f'gui/{os.getuid()}').returncode:
        raise RuntimeError('No GUI launchd domain; log in to this Mac before starting the stack.')
    inherited_cache_key = run('/bin/launchctl', 'getenv', 'AUDIO_CACHE_API_KEY').stdout.strip()
    if os.environ.get('AUDIO_CACHE_API_KEY') or inherited_cache_key:
        raise RuntimeError('AUDIO_CACHE_API_KEY is only in the caller environment and cannot be inherited safely. '
                           'Managed startup refused; keep the existing authenticated service running.')
    try:
        with urlopen('http://127.0.0.1:8000/health', timeout=2):
            pass
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError('Existing Audio Cache requires authentication; refusing to migrate without its persistent credential.') from None
    except (OSError, URLError):
        pass
    key = run('/usr/bin/security', 'find-generic-password', '-a', run('/usr/bin/id', '-un').stdout.strip(),
              '-s', os.environ.get('TTS_KEYCHAIN_SERVICE', 'com.ufun.tts.api-key'), '-w')
    stored_key = key.stdout.strip() if key.returncode == 0 else ''
    if not stored_key:
        raise RuntimeError('Existing TTS Keychain credential is unavailable; managed startup refused.')
    inherited_tts_key = run('/bin/launchctl', 'getenv', 'TTS_API_KEY').stdout.strip()
    if any(key and key != stored_key for key in (os.environ.get('TTS_API_KEY'), inherited_tts_key)):
        raise RuntimeError('TTS_API_KEY differs from the existing Keychain credential; managed startup refused.')
    for filename in ('scripts/managed-service.sh', 'scripts/text-studio-start.sh', 'scripts/recording-transcript-start.sh'):
        if not (root / filename).is_file():
            raise RuntimeError(f'Missing service entrypoint: {filename}')


def configuration(root: Path, service: Service) -> dict:
    env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'),
        'HOME': str(Path.home()), 'USER': run('/usr/bin/id', '-un').stdout.strip(),
        'PYTHONPATH': str(root), 'PYTHONUNBUFFERED': '1',
        'STACK_PYTHON': str(Path(sys.executable).resolve()),
        'TTS_KEYCHAIN_SERVICE': os.environ.get('TTS_KEYCHAIN_SERVICE', 'com.ufun.tts.api-key'),
        'RECORDING_TRANSCRIPT_PYTHON': str(Path(os.environ.get('RECORDING_TRANSCRIPT_PYTHON',
                                                   str(root / 'runtime/qwen-asr-venv/bin/python'))).absolute()),
    }
    return {
        'Label': service.label,
        'ProgramArguments': ['/bin/bash', str(root / 'scripts/managed-service.sh'), service.name],
        'WorkingDirectory': str(root), 'EnvironmentVariables': env,
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
        'ProcessType': 'Background',
        'StandardOutPath': str(root / 'runtime/logs' / f'{service.name}.log'),
        'StandardErrorPath': str(root / 'runtime/logs' / f'{service.name}.log'),
    }


def record(root: Path, message: str) -> None:
    line = f'{datetime.now().astimezone().isoformat(timespec="seconds")} {message}'
    print(line, flush=True)
    path = root / 'runtime/logs/stack-lifecycle.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        handle.write(line + '\n')


def wait_stopped(service: Service, pids: list[int], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = [run('/bin/ps', '-p', str(pid), '-o', 'stat=').stdout.strip() for pid in pids if pid > 0]
        alive = any(state and not state.startswith('Z') for state in states)
        if not alive and not set(listener_pids(service.port)).intersection(pids):
            return
        time.sleep(.25)
    raise RuntimeError(f'{service.title}: listener did not stop; refusing to replace it.')


def stop_service(root: Path, service: Service) -> None:
    info = job_info(service)
    if info and not owns_job(root, service, info):
        raise RuntimeError(f'{service.title}: launchd label belongs to another checkout; leaving it untouched.')
    listeners = listener_pids(service.port)
    if info:
        record(root, f'{service.title}: unloading PID={info.get("pid", "-")} '
               f'last-exit={info.get("last exit code", info.get("last terminating signal", "unknown"))}')
        result = run('/bin/launchctl', 'bootout', target(service))
        if result.returncode and job_info(service):
            raise RuntimeError(f'{service.title}: launchctl bootout failed ({result.returncode}).')
        wait_stopped(service, [int(info.get('pid', '0'))])
    # A retained PID file is only a hint; re-check identity before signalling it.
    pid_file = root / 'runtime/pids' / f'{service.name}.pid'
    candidates = set(listeners)
    if pid_file.is_file() and pid_file.read_text().strip().isdigit():
        candidates.add(int(pid_file.read_text().strip()))
    for pid in candidates:
        if not owns_process(root, service, pid):
            continue
        # Recheck immediately before signalling; never kill an unknown listener.
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            continue
        wait_stopped(service, [pid])
    foreign = [pid for pid in listener_pids(service.port) if not owns_process(root, service, pid)]
    if foreign:
        record(root, f'{service.title}: unrelated listener {foreign} left untouched')
    record(root, f'{service.title}: stopped (logs and PID files retained)')


def start_service(root: Path, service: Service, timeout: float = 45) -> None:
    info = job_info(service)
    if info and not owns_job(root, service, info):
        raise RuntimeError(f'{service.title}: launchd label belongs to another checkout; refusing to replace it.')
    foreign = [pid for pid in listener_pids(service.port) if not owns_process(root, service, pid)]
    if foreign:
        raise RuntimeError(f'{service.title}: port {service.port} is occupied by unrelated PID(s) {foreign}.')
    if ready(root, service, info):
        record(root, f'{service.title}: already ready PID={info["pid"]}')
        return
    if not info:
        stop_service(root, service)  # Migrate only verified legacy processes.
        path = plist_path(root, service)
        path.parent.mkdir(parents=True, exist_ok=True)
        (root / 'runtime/logs').mkdir(parents=True, exist_ok=True)
        with path.open('wb') as handle:
            plistlib.dump(configuration(root, service), handle, sort_keys=False)
        path.chmod(0o600)
        record(root, f'{service.title}: loading {path}')
        result = run('/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path))
        if result.returncode:
            raise RuntimeError(f'{service.title}: launchctl bootstrap failed ({result.returncode}): {result.stderr.strip()}')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready(root, service):
            record(root, f'{service.title}: ready PID={job_info(service).get("pid", "-")}')
            return
        time.sleep(.5)
    status(root, service)
    raise RuntimeError(f'{service.title}: health/process check timed out after {timeout:g}s; '
                       f'check {root / "runtime/logs" / (service.name + ".log")}. Other services remain running.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'start', 'stop', 'status'))
    args = parser.parse_args()
    if args.action == 'status':
        return 0 if all([status(ROOT, service) for service in SERVICES]) else 1
    try:
        if args.action in ('preflight', 'start'):
            preflight(ROOT)
        if args.action == 'preflight':
            return 0
        lock_path = ROOT / 'runtime/launchd/lifecycle.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            errors = []
            for service in (SERVICES if args.action == 'start' else reversed(SERVICES)):
                try:
                    (start_service if args.action == 'start' else stop_service)(ROOT, service)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    errors.append(str(exc))
                    record(ROOT, str(exc))
            return 1 if errors else 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Stack {args.action} failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
