import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

from audio_cache.manager import AudioCacheManager
from server.engine_runtime import ManagedEngine


def wav_bytes(duration=0.2, rate=8000):
    frames = b"\x00\x00" * int(duration * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)
    return buffer.getvalue()


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeProcess:
    def __init__(self, state):
        self.state = state
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False
        self.state["ready"] = False

    def wait(self, timeout=None):
        if self.alive:
            raise TimeoutError(timeout)
        return 0

    def kill(self):
        self.terminate()


class StageThreeEfficiencyTests(unittest.TestCase):
    def test_ack_releases_mac_wav_but_keeps_lightweight_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root, lambda audio, _metadata: audio)
            manager.add_audio(
                wav_bytes(),
                {"session_id": "live-a", "sequence": 1, "duration": 0.2},
                "segment_001",
            )
            claimed = manager.claim_next()
            self.assertIsNotNone(claimed)
            audio_path = claimed.audio_path
            self.assertTrue(audio_path.is_file())

            completed = manager.ack("segment_001", "completed")
            self.assertEqual(completed.status, "completed")
            self.assertFalse(audio_path.exists())
            self.assertEqual(manager.stats()["completed"], 1)
            self.assertEqual(manager.session_stats("live-a")["completed"], 1)

            metadata_path = root / "completed" / "segment_001" / "metadata.json"
            self.assertTrue(metadata_path.is_file())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertTrue(metadata["audio_released"])
            self.assertGreater(metadata["audio_released_bytes"], 0)

            # Hot-path stats must be served from memory rather than rescanning
            # filesystem items on every UI/client poll.
            manager.list_items = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("filesystem rescan"))
            self.assertEqual(manager.stats()["completed"], 1)
            self.assertEqual(manager.session_stats("live-a")["completed"], 1)

    def test_restart_rebuilds_memory_index_once_and_ready_order_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root, lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"session_id": "live-a", "sequence": 2}, "segment_002")
            manager.add_audio(wav_bytes(), {"session_id": "live-a", "sequence": 1}, "segment_001")
            first = manager.claim_next()
            self.assertEqual(first.id, "segment_001")
            manager.ack(first.id)

            restarted = AudioCacheManager(root, lambda audio, _metadata: audio)
            self.assertEqual(restarted.stats()["completed"], 1)
            self.assertEqual(restarted.stats()["ready"], 1)
            self.assertEqual(restarted.session_stats("live-a")["completed"], 1)
            self.assertEqual(restarted.claim_next().id, "segment_002")

    def test_session_cleanup_reclaims_terminal_metadata_after_audio_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root, lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"session_id": "live-a", "sequence": 1}, "segment_001")
            manager.ack(manager.claim_next().id)
            self.assertEqual(manager.cleanup_session("live-a"), 1)
            self.assertEqual(manager.session_stats("live-a"), {
                "pending": 0,
                "ready": 0,
                "processing": 0,
                "completed": 0,
                "failed": 0,
            })
            self.assertFalse((root / "completed" / "segment_001").exists())

    def test_managed_engine_starts_on_demand_sleeps_when_idle_and_wakes_again(self):
        state = {"ready": False, "launches": 0}
        clock = FakeClock()

        def launch():
            state["launches"] += 1
            state["ready"] = True
            return FakeProcess(state)

        engine = ManagedEngine(
            "http://engine",
            launcher=launch,
            probe=lambda: state["ready"],
            idle_seconds=4,
            startup_timeout=1,
            clock=clock,
        )
        try:
            self.assertEqual(engine.state(), "sleeping")
            self.assertTrue(engine.run(lambda restarted: restarted))
            self.assertEqual(engine.state(), "ready")
            self.assertEqual(state["launches"], 1)

            clock.advance(5)
            self.assertTrue(engine.sleep_if_idle())
            self.assertEqual(engine.state(), "sleeping")

            def protected_request(restarted):
                self.assertTrue(restarted)
                clock.advance(5)
                self.assertFalse(engine.sleep_if_idle())
                return "ok"

            self.assertEqual(engine.run(protected_request), "ok")
            self.assertEqual(state["launches"], 2)
            stats = engine.stats()
            self.assertEqual(stats["wake_count"], 2)
            self.assertEqual(stats["sleep_count"], 1)
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
