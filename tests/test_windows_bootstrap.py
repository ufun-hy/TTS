"""Bootstrap checks the signed app release without a model release source."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_bootstrap', ROOT / 'build/windows/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bootstrap)


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class BootstrapTests(unittest.TestCase):
    def test_app_install_check_does_not_require_model_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory) / 'models'
            release = {'version': '1.2.3', 'commit': 'a' * 40}
            package = {'kind': 'installer', 'size': 10, 'sha256': 'b' * 64}
            updater = mock.Mock()
            updater.package = package
            updater.check.return_value = release
            model_status = {
                'tts': {'ready': False, 'errors': ['missing']},
                'asr': {'ready': False, 'errors': ['missing']},
                'llm': {'ready': False, 'errors': ['missing']},
            }
            instance = object.__new__(bootstrap.Bootstrap)
            instance.model_path = Value(str(models))
            results = []
            instance.run = lambda action, kind: results.append((kind, action()))
            with mock.patch.object(bootstrap, 'OnlineUpdater', return_value=updater), \
                    mock.patch.object(bootstrap, 'inspect_local_models', return_value=model_status), \
                    mock.patch.object(bootstrap, '_log'):
                instance.check()
            updater.check.assert_called_once_with()
            self.assertEqual(results[0][0], 'checked')
            self.assertIs(results[0][1][1], model_status)


if __name__ == '__main__':
    unittest.main()
