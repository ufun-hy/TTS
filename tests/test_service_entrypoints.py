"""Exercise standalone imports and the default macOS Bash."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ServiceEntrypointTests(unittest.TestCase):
    def test_script_entrypoints_without_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        for script in ('text_studio.py', 'text_studio_entry.py', 'recording_transcript.py', 'tts_gateway.py'):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(root/'server'/script), '--help'],
                                        cwd=tempfile.gettempdir(),
                                        env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'},
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--port', result.stdout)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Bash entrypoint test')
    def test_text_studio_shell_entrypoint_without_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['/bin/bash', str(root/'scripts/text-studio-start.sh'), '--help'],
                                cwd='/tmp', env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--port', result.stdout)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Bash array compatibility test')
    def test_mac_bash_optional_verbose_array(self):
        root = Path(__file__).resolve().parents[1]
        # Exercise the exact expansion used in start.sh without loading credentials or models.
        source = (root/'start.sh').read_text()
        self.assertIn('${ENGINE_VERBOSE_ARGS[@]-}', source)
        for setup, count in [('ENGINE_VERBOSE_ARGS=()', '0'),
                             ('ENGINE_VERBOSE_ARGS=(--engine-verbose)', '1')]:
            result = subprocess.run(['/bin/bash', '-uc', setup + '; set -- ${ENGINE_VERBOSE_ARGS[@]-}; echo "$#"'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), count)
