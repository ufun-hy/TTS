"""Installer-only signed releases remain valid without patches or model files."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from local_runtime.online_update import verify_release


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_signer', ROOT / 'build/windows/sign_release.py')
signer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(signer)


class InstallerOnlyReleaseTests(unittest.TestCase):
    def test_signer_builds_signed_installer_release_without_patch_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = Ed25519PrivateKey.generate()
            public = base64.b64encode(key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
            private_key = root / 'release.pem'
            private_key.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            (root / 'config').mkdir()
            (root / 'config/update-source.json').write_text(
                json.dumps({'public_key': public}), encoding='utf-8')
            manifest = {'version': '1.2.3', 'release_sequence': 3, 'commit': 'a' * 40,
                        'runtime_id': 'b' * 64}
            installer = root / 'installer.zip'
            with zipfile.ZipFile(installer, 'w') as archive:
                for name, content in {
                    'AI-Live-Studio-Windows-Test-Setup.exe': b'setup',
                    'AI-Live-Studio-Windows-Test-Setup-1.bin': b'volume',
                    'app-manifest.json': json.dumps(manifest).encode(),
                    'README.md': b'docs',
                    'runtime/runtime-manifest.json': b'{}',
                    'runtime/python-lock.txt': b'package==1',
                }.items():
                    archive.writestr(name, content)
            bootstrap = root / 'AI-Live-Studio-Bootstrap.exe'
            bootstrap.write_bytes(b'bootstrap')
            notes = root / 'notes.txt'
            notes.write_text('Installer-only V1', encoding='utf-8')
            output = root / 'latest.json'
            argv = [
                'sign_release.py', '--key', str(private_key), '--installer', str(installer),
                '--installer-url', 'https://downloads.example/installer.zip',
                '--bootstrap', str(bootstrap), '--bootstrap-url', 'https://downloads.example/bootstrap.exe',
                '--notes', str(notes), '--output', str(output),
            ]
            with patch.object(signer, 'ROOT', root), patch('sys.argv', argv):
                signer.main()
            release = verify_release(json.loads(output.read_text(encoding='utf-8')), public)
            self.assertEqual([package['kind'] for package in release['packages']], ['installer'])
            self.assertEqual(release['bootstrap']['version'], release['version'])
            self.assertNotIn('models', release)


if __name__ == '__main__':
    unittest.main()
