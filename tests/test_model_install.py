import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from local_runtime.model_install import ModelInstaller, inspect_local_models, verify_model_manifest
from local_runtime.update import UpdateError

_signer_spec = importlib.util.spec_from_file_location(
    'test_sign_models', Path(__file__).resolve().parents[1] / 'build/windows/sign_models.py')
_signer = importlib.util.module_from_spec(_signer_spec)
_signer_spec.loader.exec_module(_signer)


class Response(io.BytesIO):
    pass


class ModelInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / 'models'
        self.data = self.root / 'data'
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.content = b'model bytes'
        self.manifest = {
            'schema': 1, 'product': 'AI Live Studio', 'id': 'default',
            'version': '2026.10.1', 'sequence': 1,
            'files': [{'path': 'tts/voices/default.gguf', 'url': 'https://cdn.example/model.gguf',
                       'size': len(self.content), 'sha256': hashlib.sha256(self.content).hexdigest()}],
        }
        self.config = self.root / 'model-source.json'
        self.config.write_text(json.dumps({'manifest_url': 'https://cdn.example/latest.json',
                                           'public_key': self.public}), encoding='utf-8')

    def envelope(self):
        payload = json.dumps(self.manifest, sort_keys=True, separators=(',', ':')).encode()
        return {'payload': base64.b64encode(payload).decode(),
                'signature': base64.b64encode(self.key.sign(payload)).decode()}

    def installer(self):
        return ModelInstaller(self.models, self.data, self.config)

    def test_signature_and_path_validation(self):
        self.assertEqual(verify_model_manifest(self.envelope(), self.public)['version'], '2026.10.1')
        for unsafe in ('../outside.gguf', '.downloads/escape', 'CON/file.gguf'):
            self.manifest['files'][0]['path'] = unsafe
            with self.subTest(path=unsafe), self.assertRaises(UpdateError):
                verify_model_manifest(self.envelope(), self.public)

    def test_local_readiness_is_independent_of_signed_model_source(self):
        status = inspect_local_models(self.models)
        self.assertFalse(status['tts']['ready'])
        self.assertFalse(status['asr']['ready'])
        self.assertFalse(status['llm']['ready'])

        tts = self.models / 'tts'
        (tts / 'voices').mkdir(parents=True)
        (tts / 'CosyVoice3-2512_Q8_0.gguf').write_bytes(b'local tts model')
        (tts / 'voices/default.gguf').write_bytes(b'local voice prompt')
        (tts / 'voices.json').write_text(
            json.dumps({'default': {'prompt_speech': 'voices/default.gguf'}}), encoding='utf-8')
        status = inspect_local_models(self.models)
        self.assertTrue(status['tts']['ready'])
        self.assertFalse(status['asr']['ready'])

    def test_verified_existing_model_is_reused_without_download(self):
        target = self.models / 'tts/voices/default.gguf'
        target.parent.mkdir(parents=True)
        target.write_bytes(self.content)
        installer = self.installer()
        with patch('local_runtime.model_install._open_https', return_value=Response(json.dumps(self.envelope()).encode())):
            installer.check()
        self.assertEqual(installer.status()['reused'], ['tts/voices/default.gguf'])
        with patch('local_runtime.model_install.download_artifact') as download:
            result = installer.install()
        download.assert_not_called()
        self.assertEqual(result['reused'], 1)

    def test_downloaded_file_is_verified_and_state_is_written(self):
        installer = self.installer()
        with patch('local_runtime.model_install._open_https', return_value=Response(json.dumps(self.envelope()).encode())):
            installer.check()
        cache = self.models / '.downloads'
        cache.mkdir(parents=True)
        payload = cache / (hashlib.sha256(self.content).hexdigest() + '.model')
        payload.write_bytes(self.content)
        with patch('local_runtime.model_install.download_artifact', return_value=payload):
            result = installer.install()
        self.assertEqual((self.models / 'tts/voices/default.gguf').read_bytes(), self.content)
        self.assertEqual(result['installed'], 1)
        self.assertEqual(json.loads((self.models / '.ai-live-studio-models.json').read_text())['sequence'], 1)

    def test_unmanaged_mismatched_file_is_not_overwritten(self):
        target = self.models / 'tts/voices/default.gguf'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'user file')
        installer = self.installer()
        with patch('local_runtime.model_install._open_https', return_value=Response(json.dumps(self.envelope()).encode())):
            installer.check()
        with self.assertRaisesRegex(UpdateError, '未覆盖'):
            installer.install()
        self.assertEqual(target.read_bytes(), b'user file')

    def test_release_model_selection_excludes_runtime_data(self):
        root = self.root / 'prepared-models'
        files = {
            'asr/Qwen3-ASR-1.7B/config.json': b'{"model_type":"qwen3_asr"}',
            'asr/Qwen3-ASR-1.7B/preprocessor_config.json': b'{}',
            'asr/Qwen3-ASR-1.7B/tokenizer_config.json': b'{}',
            'asr/Qwen3-ASR-1.7B/model.safetensors': b'asr weights',
            'tts/CosyVoice3-2512_Q8_0.gguf': b'tts model',
            'tts/voices.json': b'{"default":{"prompt_speech":"voices/default.gguf"}}',
            'tts/voices/default.gguf': b'default voice',
            'llm/Modelfile': b'FROM qwen3:8b',
            'llm/ollama-store/manifests/registry.ollama.ai/library/qwen3/8b': b'{}',
            'llm/ollama-store/blobs/sha256-abc': b'llm model',
            'logs/service.log': b'must not ship',
            'runtime/windows-processes.json': b'must not ship',
        }
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        selected = _signer.validate_default_models(root)
        self.assertIn('tts/voices/default.gguf', selected)
        self.assertIn('llm/ollama-store/blobs/sha256-abc', selected)
        self.assertFalse(any(name.startswith(('logs/', 'runtime/')) for name in selected))


if __name__ == '__main__':
    unittest.main()
