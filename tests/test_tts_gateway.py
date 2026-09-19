import contextlib
import io
import json
from pathlib import Path
from queue import Queue
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib import error, request

from server.tts_gateway import Gateway, RateLimiter, TTSResultCache, make_handler


class TTSGatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.voice_store = Mock()
        self.voice_store.ids.return_value = ["default"]
        self.voice_store.cache_fingerprint.return_value = "voice-v1"
        self.synthesize = Mock(return_value=b"RIFFtest")
        self.cache = TTSResultCache(Path(self.directory.name), self.voice_store)
        self.limiter = RateLimiter(30)
        queue = Queue()
        queue.put = lambda job: job.done.set()
        handler = make_handler(queue, self.voice_store, self.limiter, "test-key", Mock(), self.synthesize, 200, self.cache)

        class TestHandler(handler):
            def do_POST(self):
                # Exercise address authorization without requiring a LAN interface.
                self.client_address = (self.server.test_client_ip, self.client_address[1])
                super().do_POST()

        self.server = Gateway(("127.0.0.1", 0), TestHandler)
        self.server.audio_dir = Path(self.directory.name)
        self.server.test_client_ip = "127.0.0.1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def post(self, path, key="test-key", voice="default"):
        req = request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=json.dumps({"text": "缓存测试", "voice": voice}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            response = request.urlopen(req, timeout=2)
        except error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.headers, response.read()

    def test_synthesize_over_30_requests_does_not_consume_speak_limit(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for index in range(35):
                status, headers, audio = self.post("/synthesize")
                self.assertEqual(status, 200)
                self.assertEqual(audio, b"RIFFtest")
                self.assertEqual(headers["X-TTS-Cache"], "MISS" if index == 0 else "HIT")
            for _ in range(30):
                self.assertEqual(self.post("/speak")[0], 200)
            status, headers, _ = self.post("/speak")
            self.assertEqual(status, 429)
            self.assertEqual(headers["Retry-After"], "60")
            self.assertEqual(self.post("/synthesize")[0], 200)
        self.synthesize.assert_called_once()
        self.assertIn('"cache_hit": true', output.getvalue())
        self.assertIn('"cache_hit": false', output.getvalue())

    def test_speak_keeps_configured_limit(self):
        self.limiter.per_minute = 2
        self.assertEqual(self.post("/speak")[0], 200)
        self.assertEqual(self.post("/speak")[0], 200)
        status, headers, _ = self.post("/speak")
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)

    def test_synthesize_still_requires_key_localhost_and_known_voice(self):
        self.assertEqual(self.post("/synthesize", key="wrong")[0], 401)
        self.server.test_client_ip = "192.0.2.1"
        self.assertEqual(self.post("/synthesize")[0], 403)
        self.server.test_client_ip = "::1"
        self.assertEqual(self.post("/synthesize", voice="missing")[0], 404)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.post("/synthesize")[0], 200)


if __name__ == "__main__":
    unittest.main()
