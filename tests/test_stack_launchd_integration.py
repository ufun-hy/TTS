"""Opt-in real launchd test: RUN_LAUNCHD_TESTS=1 python3 -m unittest discover -s tests -p test_stack_launchd_integration.py.

Uses a unique launchd label, loopback port and retained temporary directory.
It never starts/stops the real TTS services and does not access Keychain.
"""
from dataclasses import asdict, replace
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from server import stack_services as stack


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('RUN_LAUNCHD_TESTS') == '1', 'opt-in launchd integration')
class LaunchdIntegrationTests(unittest.TestCase):
    def test_launcher_exit_recovery_duplicate_start_and_explicit_stop(self):
        root = Path(tempfile.mkdtemp(prefix='stack-launchd-integration-'))
        for folder in ('scripts', 'server', 'runtime/logs', 'runtime/pids'):
            (root / folder).mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        service = replace(stack.SERVICES[2], port=port, label='com.ufun.tts.test.' + uuid.uuid4().hex)
        (root / 'scripts/managed-service.sh').write_text((stack.ROOT / 'scripts/managed-service.sh').read_text())
        (root / 'scripts/recording-transcript-start.sh').write_text(
            '#!/bin/bash\nROOT="$(cd "$(dirname "$0")/.." && pwd)"\n'
            f'exec "$STACK_PYTHON" "$ROOT/server/recording_transcript.py" {port}\n')
        (root / 'server/recording_transcript.py').write_text('''
from http.server import HTTPServer, BaseHTTPRequestHandler
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
''')
        log = root / 'runtime/logs/recording-transcript.log'
        log.write_text('previous-log-sentinel\n')
        old_pid_file = root / 'runtime/pids/recording-transcript.pid'
        legacy = subprocess.Popen([sys.executable, str(root / 'server/recording_transcript.py'), str(port)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        old_pid_file.write_text(str(legacy.pid) + '\n')
        launcher = ('from pathlib import Path; from server.stack_services import Service, start_service; '
                    f'start_service(Path({str(root)!r}), Service(**{asdict(service)!r}))')
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not stack.health_ok(service):
                time.sleep(.1)
            self.assertTrue(stack.health_ok(service), 'legacy fixture failed to start')
            # The launcher exits completely before we verify the launchd-owned process.
            result = subprocess.run([sys.executable, '-c', launcher], cwd=stack.ROOT,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsNotNone(legacy.poll(), 'verified legacy process was not stopped')
            self.assertTrue(stack.ready(root, service))
            first_pid = int(stack.job_info(service)['pid'])
            stack.start_service(root, service)
            self.assertEqual(int(stack.job_info(service)['pid']), first_pid)
            print(f'Launcher exited; service survives; duplicate start kept PID {first_pid}.', flush=True)
            # Exit after a long-enough run to verify the wrapper delay, not just launchd throttling.
            time.sleep(11)
            crashed_at = time.monotonic()
            os.kill(first_pid, signal.SIGKILL)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not stack.ready(root, service):
                time.sleep(.5)
            self.assertTrue(stack.ready(root, service), stack.job_info(service))
            self.assertNotEqual(int(stack.job_info(service)['pid']), first_pid)
            self.assertGreaterEqual(time.monotonic() - crashed_at, 10)
            self.assertIn('previous-log-sentinel', log.read_text())
            self.assertGreaterEqual(log.read_text().count('starting PID='), 2)
            stack.stop_service(root, service)
            time.sleep(12)
            self.assertFalse(stack.job_info(service))
            self.assertFalse(stack.listener_pids(service.port))
            self.assertEqual(old_pid_file.read_text(), str(legacy.pid) + '\n')
            print(f'Recovery and explicit stop passed. Retained evidence: {root}', flush=True)
        finally:
            if stack.owns_job(root, service, stack.job_info(service)):
                stack.stop_service(root, service)
            if legacy.poll() is None:
                legacy.terminate()
            legacy.wait(timeout=5)
