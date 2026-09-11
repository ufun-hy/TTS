"""Exercise script imports with the same PYTHONPATH used by stack-start."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ServiceEntrypointTests(unittest.TestCase):
    def test_script_entrypoints_with_stack_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        for script in ('text_studio.py', 'text_studio_entry.py', 'recording_transcript.py'):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(root/'server'/script), '--help'],
                                        cwd=root, env={**os.environ, 'PYTHONPATH': str(root)},
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--port', result.stdout)
