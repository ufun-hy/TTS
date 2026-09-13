import io
from pathlib import Path
import tempfile
import unittest
import wave

from audio_cache.processing import AudioProcessingConfig, AudioProcessor
from server.tts_gateway import TTSResultCache


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


if __name__ == "__main__":
    unittest.main()
