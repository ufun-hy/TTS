"""Native engine launches must not depend on DYLD_* surviving system Python."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.engine_runtime import ManagedEngine


class EngineLaunchEnvironmentTests(unittest.TestCase):
    def test_macos_rebuilds_library_path_on_each_launch_with_or_without_logging(self):
        root = Path(tempfile.mkdtemp(prefix='engine-launch-env-'))
        command = [str(root / 'bin/cosyvoice-server'), '--model', '/model.gguf']
        for log_path in (None, root / 'engine.log'):
            for inherited in ('', '/custom/libraries'):
                with self.subTest(log=bool(log_path), inherited=bool(inherited)):
                    env = {'PATH': '/usr/bin:/bin', 'TEST_PRESERVED': 'yes'}
                    if inherited:
                        env['DYLD_LIBRARY_PATH'] = inherited
                    runtime = ManagedEngine('http://engine', command=command, log_path=log_path, idle_seconds=0)
                    with patch.dict(os.environ, env, clear=True), patch('server.engine_runtime.sys.platform', 'darwin'), patch('server.engine_runtime.subprocess.Popen') as popen:
                        runtime._launch()
                        runtime._launch()  # cold wake uses the same construction
                        self.assertEqual(popen.call_count, 2)
                        expected = [str((root / 'bin').resolve()), '/opt/homebrew/opt/icu4c/lib']
                        if inherited:
                            expected.append(inherited)
                        for call in popen.call_args_list:
                            self.assertEqual(call.args[0], command)
                            self.assertEqual(call.kwargs['env']['DYLD_LIBRARY_PATH'], ':'.join(expected))
                            self.assertEqual(call.kwargs['env']['TEST_PRESERVED'], 'yes')
                        self.assertEqual(dict(os.environ), env, 'parent environment was mutated')

    def test_other_platforms_keep_the_existing_environment(self):
        runtime = ManagedEngine('http://engine', command=['/bin/engine'], idle_seconds=0)
        with patch.dict(os.environ, {'PATH': '/bin'}, clear=True), patch('server.engine_runtime.sys.platform', 'linux'), patch('server.engine_runtime.subprocess.Popen') as popen:
            runtime._launch()
            self.assertEqual(popen.call_args.kwargs['env'], {'PATH': '/bin'})
