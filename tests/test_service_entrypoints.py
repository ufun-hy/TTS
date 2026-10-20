"""Exercise standalone Python entrypoints for Windows packaging."""
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

    def test_text_studio_entrypoint_adds_server_root_for_embedded_style_start(self):
        root = Path(__file__).resolve().parents[1]
        code = 'import runpy, sys; runpy.run_path(sys.argv[1], run_name="__main__")'
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        result = subprocess.run(
            [sys.executable, '-S', '-c', code, str(root / 'server/text_studio_entry.py'), '--help'],
            cwd=tempfile.gettempdir(), env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--port', result.stdout)


if __name__ == "__main__":
    unittest.main()
