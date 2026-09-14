"""Deterministic lifecycle checks; no real services or credentials are touched."""
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server import stack_services as stack


class StackServicesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='stack-services-test-'))
        self.service = stack.SERVICES[1]
        self.info = {'loaded': True, 'path': str(stack.plist_path(self.root, self.service)),
                     'pid': '123', 'state': 'running'}

    def test_plist_pins_environment_without_serializing_credentials(self):
        with patch.dict(os.environ, {'TTS_API_KEY': 'secret-tts', 'AUDIO_CACHE_API_KEY': 'secret-cache'}):
            config = stack.configuration(self.root, self.service)
        encoded = plistlib.dumps(config)
        self.assertNotIn(b'secret-', encoded)
        self.assertTrue(config['KeepAlive'])
        self.assertEqual(config['ThrottleInterval'], 10)
        self.assertEqual(config['WorkingDirectory'], str(self.root))
        self.assertTrue(Path(config['EnvironmentVariables']['STACK_PYTHON']).is_absolute())
        self.assertEqual(config['EnvironmentVariables']['PYTHONPATH'], str(self.root))
        self.assertIn('/runtime/launchd/', str(stack.plist_path(self.root, self.service)))

    def test_credentials_fail_before_any_lifecycle_mutation(self):
        calls = []
        def run(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, 'stored-key' if args[0].endswith('security') else '', '')
        with patch.object(stack, 'run', side_effect=run), patch.object(stack.sys, 'platform', 'darwin'), patch.object(stack, 'urlopen', side_effect=stack.URLError('offline')):
            with patch.dict(os.environ, {'AUDIO_CACHE_API_KEY': 'custom'}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'cannot be inherited'):
                    stack.preflight(self.root)
            with patch.dict(os.environ, {'TTS_API_KEY': 'different'}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'differs'):
                    stack.preflight(self.root)
        self.assertFalse(any('bootout' in call or 'bootstrap' in call for call in calls))
        self.assertFalse((self.root / 'runtime').exists())

    def test_authenticated_legacy_cache_is_not_migrated(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(stack.sys, 'platform', 'darwin'), patch.object(stack, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, patch.object(stack, 'urlopen', side_effect=stack.HTTPError('http://localhost/health', 401, 'unauthorized', {}, None)):
            with self.assertRaisesRegex(RuntimeError, 'requires authentication'):
                stack.preflight(self.root)
        self.assertFalse(any('bootout' in call.args or 'bootstrap' in call.args for call in run.call_args_list))

    def test_ready_requires_owned_job_process_listener_and_health(self):
        with patch.object(stack, 'owns_process', return_value=True), patch.object(stack, 'health_ok', return_value=True), patch.object(stack, 'listener_pids', return_value=[123]):
            self.assertTrue(stack.ready(self.root, self.service, self.info))
            self.assertFalse(stack.ready(self.root, self.service, {**self.info, 'path': '/another/checkout.plist'}))
            with patch.object(stack, 'listener_pids', return_value=[456]):
                self.assertFalse(stack.ready(self.root, self.service, self.info))

    def test_duplicate_start_does_not_restart_a_healthy_job(self):
        with patch.object(stack, 'job_info', return_value=self.info), patch.object(stack, 'listener_pids', return_value=[123]), patch.object(stack, 'owns_process', return_value=True), patch.object(stack, 'ready', return_value=True), patch.object(stack, 'run') as run:
            stack.start_service(self.root, self.service)
            run.assert_not_called()

    def test_foreign_port_is_never_killed_or_replaced(self):
        with patch.object(stack, 'job_info', return_value={}), patch.object(stack, 'listener_pids', return_value=[456]), patch.object(stack, 'owns_process', return_value=False), patch.object(stack.os, 'kill') as kill, patch.object(stack, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'occupied by unrelated'):
                stack.start_service(self.root, self.service)
            kill.assert_not_called()
            run.assert_not_called()
        self.assertFalse(stack.plist_path(self.root, self.service).exists())

    def test_foreign_launchd_job_is_not_unloaded(self):
        with patch.object(stack, 'job_info', return_value={**self.info, 'path': '/foreign/job.plist'}), patch.object(stack, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'another checkout'):
                stack.stop_service(self.root, self.service)
            run.assert_not_called()

    def test_stop_boots_out_before_legacy_handling_and_retains_pid(self):
        pid = self.root / 'runtime/pids/text-studio.pid'
        pid.parent.mkdir(parents=True)
        pid.write_text('456\n')  # stale PID reused by an unrelated process
        with patch.object(stack, 'job_info', return_value=self.info), patch.object(stack, 'listener_pids', return_value=[]), patch.object(stack, 'owns_process', return_value=False), patch.object(stack, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, patch.object(stack.os, 'kill') as kill:
            stack.stop_service(self.root, self.service)
            self.assertEqual(run.call_args_list[0].args, ('/bin/launchctl', 'bootout', stack.target(self.service)))
            kill.assert_not_called()
        self.assertEqual(pid.read_text(), '456\n')

    def test_timeout_reports_failure_and_does_not_unload_job(self):
        with patch.object(stack, 'job_info', return_value=self.info), patch.object(stack, 'listener_pids', return_value=[]), patch.object(stack, 'ready', return_value=False), patch.object(stack, 'status'), patch.object(stack, 'stop_service') as stop:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                stack.start_service(self.root, self.service, timeout=0)
            stop.assert_not_called()

    def test_one_failure_does_not_skip_other_services(self):
        with patch.object(stack, 'ROOT', self.root), patch.object(stack.sys, 'argv', ['stack-services', 'start']), patch.object(stack, 'preflight'), patch.object(stack, 'start_service', side_effect=[RuntimeError('first failed'), None, None]) as start:
            self.assertEqual(stack.main(), 1)
            self.assertEqual(start.call_count, 3)

    def test_script_identity_matches_whole_paths(self):
        script = str(self.root / self.service.markers[0])
        for command, expected in [(f'/usr/bin/python3 {script} --port 8770', True), (f'/usr/bin/python3 {script}.other', False), ('/usr/bin/python3 /other/server/text_studio_entry.py', False)]:
            with patch.object(stack, 'run', return_value=subprocess.CompletedProcess([], 0, command, '')):
                self.assertEqual(stack.owns_process(self.root, self.service, 123), expected)

    def test_logs_are_appended(self):
        stack.record(self.root, 'first')
        stack.record(self.root, 'second')
        log = (self.root / 'runtime/logs/stack-lifecycle.log').read_text()
        self.assertIn('first', log)
        self.assertIn('second', log)
