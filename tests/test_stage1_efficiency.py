import io
from pathlib import Path
import json
from queue import Queue
import tempfile
import threading
import unittest
import wave
from urllib.request import urlopen

from audio_cache.processing import AudioProcessingConfig, AudioProcessor
from server.engine_runtime import ManagedEngine
from server.tts_gateway import Gateway, RateLimiter, TTSResultCache, make_handler


def wav_bytes(duration=0.25, rate=8000):
    frames = b"\x00\x00" * int(duration * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)
    return buffer.getvalue()


class FakeVoiceStore:
    def __init__(self):
        self.fingerprint = "voice-v1"

    def cache_fingerprint(self, _voice):
        return self.fingerprint


class StageOneEfficiencyTests(unittest.TestCase):
    def test_tts_result_cache_reuses_audio_and_invalidates_on_voice_change(self):
        with tempfile.TemporaryDirectory() as directory:
            voice_store = FakeVoiceStore()
            cache = TTSResultCache(Path(directory), voice_store, revision="model-v1")
            calls = []

            def produce():
                calls.append(1)
                return wav_bytes()

            first, first_hit = cache.get_or_create("同一句直播文案", "default", produce)
            second, second_hit = cache.get_or_create("同一句直播文案", "default", produce)

            self.assertFalse(first_hit)
            self.assertTrue(second_hit)
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            self.assertEqual(cache.stats(), {"hits": 1, "misses": 1})

            voice_store.fingerprint = "voice-v2"
            third, third_hit = cache.get_or_create("同一句直播文案", "default", produce)
            self.assertFalse(third_hit)
            self.assertEqual(third, first)
            self.assertEqual(len(calls), 2)
            self.assertEqual(cache.stats(), {"hits": 1, "misses": 2})
            self.assertEqual(len(list(Path(directory).rglob("*.wav"))), 2)

    def test_live_default_audio_bypasses_ffmpeg_and_preserves_source(self):
        source = wav_bytes()
        processor = AudioProcessor(AudioProcessingConfig(), ffmpeg="Z:/definitely-missing/ffmpeg.exe")
        result = processor.process(source, {
            "source": "live_session",
            "session_id": "live-001",
            "playback_speed": 1.0,
            "volume": 100,
        })

        self.assertEqual(result.audio, source)
        self.assertAlmostEqual(result.raw_duration, 0.25, places=2)
        self.assertAlmostEqual(result.duration, 0.25, places=2)
        self.assertEqual(result.speed_factor, 1.0)
        self.assertEqual(result.session_volume, 100.0)
        self.assertEqual(result.session_volume_gain, 0.0)

    def test_cache_stats_do_not_wait_for_slow_synthesis(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TTSResultCache(Path(directory), FakeVoiceStore(), revision="model-v1")
            started = threading.Event()
            release = threading.Event()
            stats_done = threading.Event()
            result = {}

            def produce():
                started.set()
                release.wait(2)
                return wav_bytes()

            worker = threading.Thread(
                target=lambda: result.setdefault("value", cache.get_or_create("慢合成", "default", produce)),
            )
            worker.start()
            self.assertTrue(started.wait(1))
            threading.Thread(target=lambda: (cache.stats(), stats_done.set())).start()
            self.assertTrue(stats_done.wait(0.2), "cache statistics waited for the producer")
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(result["value"][1])
            self.assertTrue(cache.get_or_create("慢合成", "default", lambda: wav_bytes())[1])

    def test_gateway_health_does_not_wait_for_slow_synthesis(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TTSResultCache(Path(directory), FakeVoiceStore(), revision="model-v1")
            started = threading.Event()
            release = threading.Event()
            producer = threading.Thread(
                target=lambda: cache.get_or_create(
                    "慢合成", "default", lambda: (started.set(), release.wait(2), wav_bytes())[2]
                )
            )
            producer.start()
            self.assertTrue(started.wait(1))

            worker_stop = threading.Event()
            worker = threading.Thread(target=worker_stop.wait)
            worker.start()
            engine = ManagedEngine("http://engine", probe=lambda: True)
            server = Gateway(("127.0.0.1", 0), make_handler(
                Queue(), FakeVoiceStore(), RateLimiter(30), "key", engine,
                lambda _text, _voice: wav_bytes(), 200, cache,
            ))
            server.worker_thread = worker  # type: ignore[attr-defined]
            serving = threading.Thread(target=server.serve_forever)
            serving.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=0.5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response)["status"], "ok")
            finally:
                server.shutdown()
                server.server_close()
                serving.join(1)
                release.set()
                producer.join(2)
                worker_stop.set()
                worker.join(1)


if __name__ == "__main__":
    unittest.main()
