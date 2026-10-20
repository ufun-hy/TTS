"""Windows TTS client never reads macOS Keychain and preserves /synthesize contract."""
import os
import unittest
from unittest.mock import patch
from server.tts_client import TTSClient, TTSClientError, load_api_key


class TTSClientMigrationTests(unittest.TestCase):
    def test_key_only_comes_from_runtime_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_api_key(), "")
        with patch.dict(os.environ, {"TTS_API_KEY": " runtime-key "}, clear=True):
            self.assertEqual(load_api_key(), "runtime-key")

    def test_synthesis_uses_auth_header_and_local_gateway(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"RIFFmock-wave"

        def fake_open(req, timeout):
            self.assertEqual(req.full_url, "http://127.0.0.1:8765/synthesize")
            self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
            self.assertEqual(timeout, 12)
            return Response()

        with patch("server.tts_client.request.urlopen", side_effect=fake_open):
            wav, duration = TTSClient(api_key="test-key", timeout=12).synthesize("测试", "voice1")
        self.assertEqual(wav, b"RIFFmock-wave")
        self.assertGreaterEqual(duration, 0)

    def test_non_wav_fails(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"not WAV"

        with patch("server.tts_client.request.urlopen", return_value=Response()):
            with self.assertRaises(TTSClientError):
                TTSClient(api_key="test-key").synthesize("test", "voice1", retries=0)


if __name__ == "__main__":
    unittest.main()
