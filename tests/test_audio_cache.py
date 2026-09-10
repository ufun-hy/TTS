import io
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest
import wave

from audio_client.client import AudioClient
from audio_client.config import ClientConfig
from audio_cache.manager import AudioCacheManager
from audio_cache.processing import AudioProcessingConfig, calculate_speed_factor, calculate_volume_gain
from audio_cache.server import AudioCacheServer, make_handler


def wav_bytes(duration=0.1, rate=8000):
    frames = b"\x00\x00" * int(duration * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)
    return buffer.getvalue()


def tone_wav_bytes(duration=1.0, rate=8000):
    buffer = io.BytesIO()
    frames = b"".join(int(10000 * math.sin(2 * math.pi * 440 * index / rate)).to_bytes(2, "little", signed=True) for index in range(int(duration * rate)))
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)
    return buffer.getvalue()


class AudioCacheTests(unittest.TestCase):
    def test_state_flow_keeps_order_and_ack_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AudioCacheManager(Path(directory), lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"text": "第二段", "sequence": 2}, "segment_002")
            manager.add_audio(wav_bytes(), {"text": "第一段", "sequence": 1}, "segment_001")
            first = manager.claim_next()
            self.assertEqual(first.id, "segment_001")
            completed = manager.ack(first.id)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(manager.ack(first.id).status, "completed")
            self.assertEqual(manager.claim_next().id, "segment_002")

    def test_failed_processing_is_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail(_audio, _metadata):
                raise ValueError("bad audio")

            manager = AudioCacheManager(Path(directory), fail)
            with self.assertRaises(ValueError):
                manager.add_audio(b"not wav", {"text": "失败"}, "segment_001")
            self.assertEqual(manager.stats()["failed"], 1)
            metadata = json.loads((Path(directory) / "failed/segment_001/metadata.json").read_text())
            self.assertEqual(metadata["status"], "failed")

    def test_session_stats_and_cleanup_are_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AudioCacheManager(Path(directory), lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"session_id": "live_a"}, "segment_001")
            manager.add_audio(wav_bytes(), {"session_id": "live_b"}, "segment_002")
            self.assertEqual(manager.session_stats("live_a")["ready"], 1)
            self.assertEqual(manager.cleanup_session("live_a"), 1)
            self.assertFalse(manager.has("segment_001"))
            self.assertTrue(manager.has("segment_002"))

    def test_processing_limits_are_reported(self):
        config = AudioProcessingConfig()
        factor, warnings = calculate_speed_factor(40, 30, config)
        self.assertEqual(factor, 1.15)
        self.assertIn("speed_factor_out_of_range", warnings)
        gain, warnings = calculate_volume_gain(-30, config)
        self.assertEqual(gain, 3)
        self.assertIn("volume_gain_out_of_range", warnings)

    def test_session_speed_is_applied_on_mac_processor(self):
        from audio_cache.processing import AudioProcessor

        processor = AudioProcessor(AudioProcessingConfig(volume_enabled=False))
        for speed in (1.0, 1.05, 1.10):
            result = processor.process(tone_wav_bytes(), {"playback_speed": speed, "volume": 100})
            self.assertTrue(result.audio.startswith(b"RIFF"))
            self.assertAlmostEqual(result.duration, 1 / speed, delta=0.02)

    def test_session_volume_is_applied_without_changing_source(self):
        from audio_cache.processing import AudioProcessor

        source = tone_wav_bytes()
        result = AudioProcessor(AudioProcessingConfig(speed_enabled=False)).process(source, {"playback_speed": 1, "volume": 50})
        self.assertNotEqual(result.audio, source)
        self.assertAlmostEqual(result.session_volume_gain, -6.0206, places=2)

    def test_http_client_downloads_and_acknowledges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"sequence": 1}, "segment_001")
            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", root / "client")
                self.assertEqual(client.health()["status"], "ok")
                item = client.fetch_next()
                self.assertEqual(item.id, "segment_001")
                self.assertTrue(item.path.is_file())
                self.assertEqual(json.loads((root / "client/segment_001.json").read_text())["playback_status"], "cached")
                self.assertEqual(client.ack(item.id)["status"], "completed")
                self.assertEqual(manager.stats()["completed"], 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_client_config_accepts_install_format_and_counts_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            config = ClientConfig.from_dict({"server": "http://server:8000", "cache_dir": "./cache", "poll_interval": 1})
            self.assertEqual(ClientConfig.from_dict({"server": "http://server:8000", "poll_interval": 1000}).poll_interval, 1)
            (cache / "segment_001.wav").write_bytes(wav_bytes())
            (cache / "segment_001.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            self.assertEqual(AudioClient(config.server, cache).local_stats(), {"received": 1, "completed": 1, "failed": 0, "cache": 1})


if __name__ == "__main__":
    unittest.main()
