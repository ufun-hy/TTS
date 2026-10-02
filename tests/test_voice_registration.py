"""Recording registration boundaries and live-picker integration (no GPU needed)."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave

from local_runtime import RuntimeStage
from server import text_studio
from server.tts_gateway import VoiceStore
from voice_datasets.registration import VoiceRegistration, MAX_UPLOAD_BYTES


def wav(path, seconds=10):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x10\x00' * (seconds * 24000))


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        # Keep isolated test artifacts, including reference audio and backups.
        self.root = Path(tempfile.mkdtemp(prefix='voice-registration-test-'))
        self.service = VoiceRegistration(self.root / 'data', self.root / 'models', self.root / 'bin')
        for path in (self.service.cli, self.service.ffmpeg, self.service.tts / 'speech_tokenizer_v3.int8.onnx',
                     self.service.tts / 'campplus.int8.onnx'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.service.config.write_text(json.dumps({'default': {'prompt_speech': 'default.gguf'}}), encoding='utf-8')
        (self.service.tts / 'default.gguf').write_bytes(b'GGUF' + b'\0' * 24)

    def run_cli(self, command, log, timeout):
        if '--frontend-only' in command:
            self.assertIn('You are a helpful assistant.<|endofprompt|>大家好。', command)
            Path(command[-1]).write_bytes(b'GGUF' + b'\0' * 24)
        else:
            wav(Path(command[-1]))

    def draft(self):
        with mock.patch.object(self.service, '_run', side_effect=self.run_cli):
            return self.service.upload(io.BytesIO(b'input'), 5, '中文录音.wav')['draft_id']

    def test_registration_is_additive_idempotent_and_refreshes_labels(self):
        draft = self.draft()
        store = VoiceStore(self.root, self.service.config, 'http://unused')
        self.assertEqual(store.ids(), ['default'])
        before = self.service.config.read_bytes()
        with mock.patch.object(self.service, '_run', side_effect=self.run_cli) as run:
            result = self.service.register(draft, '黄桃女声', '大家好。')
            self.assertEqual(self.service.register(draft, '黄桃女声', '大家好。'), result)
            self.assertEqual(run.call_count, 1)
        config = json.loads(self.service.config.read_text())
        self.assertEqual(config['default'], json.loads(before)['default'])
        self.assertEqual(store.public_voices()[-1]['label'], '黄桃女声')
        backup = next(self.service.directory(draft).glob('voices-before-*.json'))
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual((self.service.directory(draft) / 'source.wav').read_bytes(), b'input')

    def test_atomic_replacement_with_same_timestamp_and_size_refreshes_labels(self):
        config = {'default': {'prompt_speech': 'default.gguf', 'label': 'old'}}
        self.service.config.write_text(json.dumps(config), encoding='utf-8')
        store = VoiceStore(self.root, self.service.config, 'http://unused')
        self.assertEqual(store.public_voices()[0]['label'], 'old')
        before = self.service.config.stat()
        config['default']['label'] = 'new'
        replacement = self.service.config.with_suffix('.tmp')
        replacement.write_text(json.dumps(config), encoding='utf-8')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.service.config)
        self.assertEqual(self.service.config.stat().st_size, before.st_size)
        self.assertEqual(store.public_voices()[0]['label'], 'new')

    def test_invalid_recording_or_input_does_not_change_config(self):
        for length, name, payload in [(MAX_UPLOAD_BYTES + 1, 'x.wav', b''), (0, 'x.wav', b''),
                                       (1, 'x.exe', b'x'), (3, 'x.wav', b'x')]:
            with self.assertRaises(ValueError):
                self.service.upload(io.BytesIO(payload), length, name)
        for duration in (2, 31):
            with mock.patch.object(self.service, '_run', side_effect=lambda cmd, *_: wav(Path(cmd[-1]), duration)):
                with self.assertRaisesRegex(ValueError, '3～30'):
                    self.service.upload(io.BytesIO(b'input'), 5, 'input.wav')
        draft = self.draft()
        for name, text in [('', 'hello'), ('名字', ''), ('a' * 61, 'hello'), ('名字', '<|endofprompt|>你好')]:
            with self.assertRaises(ValueError):
                self.service.register(draft, name, text)
        with self.assertRaises(ValueError):
            self.service.directory('../outside')
        self.assertEqual(list(json.loads(self.service.config.read_text())), ['default'])

    def test_failures_and_external_config_edits_are_not_published(self):
        draft = self.draft()
        before = self.service.config.read_bytes()
        with mock.patch.object(self.service, '_run', side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):
                self.service.register(draft, '新声音', '大家好。')
        self.assertEqual(self.service.config.read_bytes(), before)
        def edited(command, log, timeout):
            self.run_cli(command, log, timeout)
            self.service.config.write_bytes(before + b'\n')
        with mock.patch.object(self.service, '_run', side_effect=edited):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                self.service.register(draft, '新声音', '大家好。')
        self.assertEqual(self.service.config.read_bytes(), before + b'\n')
        with mock.patch.object(self.service, '_run', side_effect=lambda cmd, *_: Path(cmd[-1]).write_bytes(b'bad')):
            with self.assertRaisesRegex(ValueError, '有效音色'):
                self.service.register(draft, '新声音', '大家好。')

    def test_subprocess_timeout_preserves_log(self):
        log = self.root / 'failed.log'
        with mock.patch('voice_datasets.registration.subprocess.run', side_effect=subprocess.TimeoutExpired('cli', 180)):
            with self.assertRaisesRegex(ValueError, '超时'):
                self.service._run(['cli'], log, 180)
        self.assertTrue(log.exists())

    def test_http_local_security_busy_release_and_success(self):
        from server import voice_registration as http_api
        import local_runtime
        runtime = local_runtime.RuntimeManager(lock_path=str(self.root / 'gpu.json'))
        # Use the real Studio router, replacing only the native model service and engine stop.
        with mock.patch.dict(os.environ, {'WINDOWS_SINGLE_MACHINE': '1', 'AI_LIVE_STUDIO_DATA': str(self.root / 'data')}), \
             mock.patch('voice_datasets.registration.VoiceRegistration', return_value=self.service), \
             mock.patch.object(text_studio, 'RuntimeManager', return_value=runtime):
            handler = text_studio.make_handler(self.root, 'http://unused', tts_api_key='test')
        server = text_studio.StudioServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/voice-registration'
        def post(path, body, headers=None):
            req = Request(base + path, data=json.dumps(body).encode(),
                          headers=headers or {'Content-Type': 'application/json'})
            with urlopen(req, timeout=5) as response:
                return json.load(response)
        try:
            with urlopen(base) as response:
                self.assertTrue(json.load(response)['ready'])
            with self.assertRaises(HTTPError) as error:
                post('/register', {}, {'Origin': 'http://attacker.example', 'Content-Type': 'application/json'})
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                post('/register', {}, {'Host': 'attacker.example', 'Content-Type': 'application/json'})
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                post('/register', {'confirmed': False})
            self.assertEqual(error.exception.code, 400)
            with mock.patch.object(self.service, '_run', side_effect=self.run_cli):
                upload = Request(base + '/upload', data=b'input', headers={'X-Filename': 'test.wav'})
                with urlopen(upload) as response:
                    draft = json.load(response)['draft_id']
            payload = {'draft_id': draft, 'name': '黄桃', 'transcript': '大家好。', 'confirmed': True}
            lease = runtime.acquire(RuntimeStage.LIVE, 'live')
            with self.assertRaises(HTTPError) as error:
                post('/register', payload)
            self.assertEqual(error.exception.code, 409)
            runtime.release(lease)
            with mock.patch.object(text_studio, '_stop_tts_engine', return_value=False):
                with self.assertRaises(HTTPError):
                    post('/register', payload)
            self.assertEqual(runtime.snapshot()['state'], 'ERROR')
            with mock.patch.object(text_studio, '_stop_tts_engine', return_value=True):
                self.assertTrue(runtime.retry_release(runtime.snapshot()['operation']))
                with mock.patch.object(self.service, '_run', side_effect=self.run_cli):
                    result = post('/register', payload)
                    self.assertTrue(result['registered'])
            self.assertEqual(runtime.snapshot()['state'], 'IDLE')
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
