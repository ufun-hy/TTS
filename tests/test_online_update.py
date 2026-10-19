import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from local_runtime.online_update import OnlineUpdater, verify_release
from local_runtime.update import UpdateError


class Response(io.BytesIO):
    status = 200
    headers = {}


class OnlineUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'config').mkdir()
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        (self.root / 'config/update-source.json').write_text(json.dumps(dict(manifest_url='https://example.test/latest.json', public_key=self.public)))
        (self.root / 'app-manifest.json').write_text(json.dumps(dict(commit='a'*40, runtime_id='a'*64, release_sequence=1)))
        self.release = dict(schema=1, version='1.2.0', sequence=2, commit='b'*40, notes='修复', packages=[dict(kind='patch', size=3, sha256=hashlib.sha256(b'abc').hexdigest(), runtime_id='a'*64, url='https://example.test/patch.zip')])
        self.updater = OnlineUpdater(self.root, self.root / 'data')

    def envelope(self):
        payload = json.dumps(self.release).encode()
        return dict(payload=base64.b64encode(payload).decode(), signature=base64.b64encode(self.key.sign(payload)).decode())

    def check(self):
        with patch('local_runtime.online_update._open_https', return_value=Response(json.dumps(self.envelope()).encode())):
            return self.updater.check()

    def test_signature_rejects_tampered_payload_and_wrong_key(self):
        envelope = self.envelope()
        self.assertEqual(verify_release(envelope, self.public)['sequence'], 2)
        envelope['payload'] = base64.b64encode(b'{}').decode()
        with self.assertRaises(UpdateError):
            verify_release(envelope, self.public)
        with self.assertRaises(UpdateError):
            verify_release(self.envelope(), base64.b64encode(b'x'*32).decode())

    def test_missing_crypto_has_actionable_error(self):
        import builtins
        original = builtins.__import__
        def import_without_crypto(name, *args, **kwargs):
            if name.startswith('cryptography'):
                raise ImportError('missing dependency')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=import_without_crypto):
            with self.assertRaisesRegex(UpdateError, 'cryptography.*缺失'):
                verify_release(self.envelope(), self.public)

    def test_malformed_release_is_rejected_but_internal_errors_propagate(self):
        for envelope in [{}, {'payload': '!', 'signature': '!'}, None]:
            with self.subTest(envelope=envelope), self.assertRaises(UpdateError):
                verify_release(envelope, self.public)
        with patch('local_runtime.online_update.json.loads', side_effect=RuntimeError('internal failure')):
            with self.assertRaisesRegex(RuntimeError, 'internal failure'):
                verify_release(self.envelope(), self.public)

    def test_no_downgrade_and_incompatible_patch_uses_full_bundle(self):
        self.release['sequence'] = 1
        self.assertIsNone(self.check())
        self.release['sequence'] = 2
        self.release['packages'][0]['runtime_id'] = 'b'*64
        with self.assertRaisesRegex(UpdateError, '完整安装包'):
            self.check()
        self.release['packages'].append({**self.release['packages'][0], 'kind': 'installer'})
        self.check()
        self.assertEqual(self.updater.package['kind'], 'installer')

    def test_clean_bootstrap_selects_full_bundle_and_validates_its_own_metadata(self):
        self.release['packages'].append({**self.release['packages'][0], 'kind': 'installer'})
        self.release['bootstrap'] = dict(version=self.release['version'], url='https://example.test/bootstrap.exe', size=4,
                                        sha256=hashlib.sha256(b'boot').hexdigest())
        bootstrap = OnlineUpdater(self.root, self.root / 'bootstrap-data', current={})
        with patch('local_runtime.online_update._open_https', return_value=Response(json.dumps(self.envelope()).encode())):
            checked = bootstrap.check()
        self.assertEqual(checked['version'], '1.2.0')
        self.assertEqual(bootstrap.package['kind'], 'installer')
        self.assertEqual(verify_release(self.envelope(), self.public)['bootstrap']['size'], 4)

    def test_partial_download_resumes_and_verified_cache_reused(self):
        self.check()
        directory = self.updater.data / 'updates/downloads'
        directory.mkdir(parents=True)
        (directory / (self.updater.package['sha256'] + '.partial')).write_bytes(b'a')
        response = Response(b'bc'); response.status=206; response.headers={'Content-Range':'bytes 1-2/3'}
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            target = self.updater.download()
            self.assertEqual(opener.return_value.open.call_args.args[0].get_header('Range'), 'bytes=1-')
        self.assertEqual(target.read_bytes(), b'abc')
        with patch('urllib.request.build_opener') as opener:
            self.assertEqual(self.updater.download(), target)
            opener.assert_not_called()

    def test_complete_verified_partial_is_promoted_without_network(self):
        self.check()
        directory = self.updater.data / 'updates/downloads'
        directory.mkdir(parents=True)
        partial = directory / (self.updater.package['sha256'] + '.partial')
        partial.write_bytes(b'abc')
        with patch('urllib.request.build_opener') as opener:
            target = self.updater.download()
        opener.assert_not_called()
        self.assertEqual(target.read_bytes(), b'abc')
        self.assertFalse(partial.exists())

    def test_oversized_partial_restarts_from_zero(self):
        self.check()
        directory = self.updater.data / 'updates/downloads'
        directory.mkdir(parents=True)
        partial = directory / (self.updater.package['sha256'] + '.partial')
        partial.write_bytes(b'oversized')
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response(b'abc')
            target = self.updater.download()
        request = opener.return_value.open.call_args.args[0]
        self.assertIsNone(request.get_header('Range'))
        self.assertEqual(target.read_bytes(), b'abc')

    def test_bad_response_does_not_modify_verified_target(self):
        self.check()
        directory = self.updater.data / 'updates/downloads'
        directory.mkdir(parents=True)
        target = directory / (self.updater.package['sha256'] + '.zip')
        target.write_bytes(b'abc')
        (directory / (self.updater.package['sha256'] + '.partial')).write_bytes(b'oversized')
        with patch('urllib.request.build_opener') as opener:
            self.assertEqual(self.updater.download(), target)
        opener.assert_not_called()
        self.assertEqual(target.read_bytes(), b'abc')

    def test_range_response_cannot_extend_past_manifest_size(self):
        self.check()
        directory = self.updater.data / 'updates/downloads'
        directory.mkdir(parents=True)
        partial = directory / (self.updater.package['sha256'] + '.partial')
        partial.write_bytes(b'a')
        response = Response(b'bcd')
        response.status = 206
        response.headers = {'Content-Range': 'bytes 1-2/3'}
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(UpdateError, '超过清单大小'):
                self.updater.download()
        self.assertEqual(partial.read_bytes(), b'a')

    def test_server_ignores_range_restarts_instead_of_appending(self):
        self.check()
        directory=self.updater.data/'updates/downloads'; directory.mkdir(parents=True)
        (directory/(self.updater.package['sha256']+'.partial')).write_bytes(b'a')
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=Response(b'abc')
            self.assertEqual(self.updater.download().read_bytes(), b'abc')

    def test_corruption_never_becomes_installable(self):
        self.check()
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=Response(b'bad')
            with self.assertRaisesRegex(UpdateError, 'SHA256'):
                self.updater.download()
        self.assertFalse(list((self.updater.data/'updates/downloads').glob('*.zip')))

    def test_invalid_range_is_rejected(self):
        self.check()
        response=Response(b'abc');response.status=206;response.headers={'Content-Range':'bytes 1-3/4'}
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=response
            with self.assertRaisesRegex(UpdateError, '范围'):
                self.updater.download()

    def archive(self, entries):
        archive=self.root/'test.zip'
        with zipfile.ZipFile(archive,'w') as bundle:
            for name,content in entries.items():
                entry = zipfile.ZipInfo(name)
                # ZipInfo normalizes os.sep on Windows; preserve the adversarial bytes.
                entry.filename = name
                bundle.writestr(entry, content)
        self.updater.release=self.release
        self.updater.package={**self.release['packages'][0], 'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
        return archive

    def test_zip_traversal_and_case_collisions_rejected(self):
        for entries in ({'../escape':'x'}, {'a':'x','A':'y'}, {'C:/evil':'x'}, {'a\\b':'x'}):
            with self.subTest(entries=entries), self.assertRaisesRegex(UpdateError, '不安全路径'):
                self.updater.unpack(self.archive(entries))
        self.assertFalse((self.root/'escape').exists())

    def test_unpack_checks_release_identity_and_rehashes(self):
        entries={'payload/app-manifest.json':json.dumps(dict(commit='b'*40,runtime_id='a'*64,release_sequence=2)), 'Apply-Patch.ps1':'# entry'}
        archive=self.archive(entries)
        self.assertTrue((self.updater.unpack(archive)/'Apply-Patch.ps1').exists())
        archive.write_bytes(b'changed')
        with self.assertRaises(UpdateError):self.updater.unpack(archive)
        entries['payload/app-manifest.json']=json.dumps(dict(commit='c'*40,runtime_id='a'*64))
        with self.assertRaises(UpdateError):self.updater.unpack(self.archive(entries))

    def test_idle_check_refuses_active_tasks_and_unknown_owners(self):
        from local_runtime.online_update import ensure_idle
        from types import SimpleNamespace
        stopped = dict(processes={'studio': dict(running=False, owned=False)})
        with patch('subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(stopped))):
            ensure_idle(self.root, self.root/'data')
        active = dict(processes={'studio': dict(running=True, owned=True)})
        with patch('subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(active))), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [Response(json.dumps(v).encode()) for v in (
                {'status':'running'}, {'state':'IDLE','model_released':True}, {'jobs':[]},
                {'status':'idle'}, {'status':'stopped','client_state':{'playback_status':'idle'}})]
            with self.assertRaisesRegex(UpdateError, '仍有'):
                ensure_idle(self.root, self.root/'data')
        active['processes']['studio']['owned']=False
        with patch('subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(active))):
            with self.assertRaisesRegex(UpdateError, '归属'):
                ensure_idle(self.root, self.root/'data')
        unknown = dict(processes={'studio': {}})
        with patch('subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(unknown))):
            with self.assertRaisesRegex(UpdateError, '无法确认运行状态'):
                ensure_idle(self.root, self.root/'data')

    def test_idle_check_refuses_separate_playback(self):
        from local_runtime.online_update import ensure_idle
        from types import SimpleNamespace
        active = dict(processes={'studio': dict(running=True, owned=True)})
        with patch('subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(active))), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [Response(json.dumps(v).encode()) for v in (
                {'status':'stopped','client_buffered_segments':0,'client_buffered_seconds':0},
                {'state':'IDLE','model_released':True}, {'jobs':[]}, {'status':'idle'},
                {'status':'running','client_state':{'playback_status':'playing'}})]
            with self.assertRaisesRegex(UpdateError, '播放'):
                ensure_idle(self.root, self.root/'data')

    def test_missing_source_is_actionable(self):
        self.updater.config={}
        with self.assertRaisesRegex(UpdateError, '尚未配置'):self.updater.check()


if __name__ == '__main__':unittest.main()
