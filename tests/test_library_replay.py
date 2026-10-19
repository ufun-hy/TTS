"""Archive replay should not synthesize or mutate the original WAV library."""
import tempfile
import unittest
from pathlib import Path

from audio_cache.manager import AudioCacheManager
from audio_cache.processing import ProcessingResult
from audio_cache.server import AudioCacheServer


class LibraryReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = AudioCacheManager(
            Path(self.temp.name), processor=lambda audio, metadata: ProcessingResult(audio, raw_duration=1.0, duration=1.0)
        )
        self.server = AudioCacheServer(("127.0.0.1", 0), lambda *args, **kwargs: None)
        for i in range(2):
            self.manager.add_audio(
                b"RIFF" + bytes([i]),
                {"source": "live_session", "session_id": "generated", "sequence": i + 1},
                f"original{i}"
            )

    def tearDown(self):
        self.server.server_close()
        self.temp.cleanup()

    def test_replay_stages_original_wav_without_resynthesis(self):
        result = self.server.replay_start(self.manager, ["original1", "original0"])
        replay_session = result["session_id"]
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.manager.session_stats(replay_session)["ready"], 2)
        first = self.server.claim_next(self.manager, replay_session)
        self.assertEqual(first.metadata["library_id"], "original1")
        self.assertEqual(first.audio_path.read_bytes(), b"RIFF\x01")
        self.manager.ack(first.id)
        self.assertEqual(self.manager.library.file("original1").read_bytes(), b"RIFF\x01")
        self.assertEqual(self.server.replay_control(self.manager, "pause")["status"], "paused")
        self.assertIsNone(self.server.claim_next(self.manager, replay_session))
        self.assertEqual(self.server.replay_control(self.manager, "resume")["status"], "running")
        self.assertEqual(self.server.replay_control(self.manager, "stop")["status"], "stopped")
        self.assertEqual(len(self.manager.library.list()), 2)

    def test_replay_recovers_paused_and_requires_explicit_resume(self):
        first = self.server.replay_start(self.manager, ["original0", "original1"])
        session = first["session_id"]
        self.assertEqual(first["status"], "running")
        # Simulate a restart with the same filesystem, without duplicating WAVs.
        self.server.server_close()
        self.server = AudioCacheServer(("127.0.0.1", 0), lambda *args, **kwargs: None)
        fresh = AudioCacheManager(
            Path(self.temp.name), processor=lambda data, meta: ProcessingResult(data, raw_duration=1.0, duration=1.0)
        )
        recovered = self.server.replay_status(fresh)
        self.assertEqual(recovered["session_id"], session)
        self.assertEqual(recovered["status"], "paused")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["cache"]["ready"], 2)
        self.assertIsNone(self.server.claim_next(fresh, session))
        continued = self.server.replay_control(fresh, "resume")
        self.assertEqual(continued["status"], "running")
        self.assertFalse(continued["recovered"])
        self.assertEqual(self.server.claim_next(fresh, session).metadata["library_id"], "original0")

    def test_stopped_replay_remains_stopped_after_restart(self):
        first = self.server.replay_start(self.manager, ["original1"])
        self.server.replay_control(self.manager, "stop")
        self.server.server_close()
        self.server = AudioCacheServer(("127.0.0.1", 0), lambda *args, **kwargs: None)
        resumed = self.server.replay_status(self.manager)
        self.assertEqual(resumed["session_id"], first["session_id"])
        self.assertEqual(resumed["status"], "stopped")
        with self.assertRaisesRegex(ValueError, "already stopped"):
            self.server.replay_control(self.manager, "resume")

    def test_unknown_replay_session_does_not_autoplay_after_restart(self):
        self.assertEqual(self.server.session_control("replay_old_session")["status"], "stopped")
        self.assertEqual(self.server.session_control("ordinary_live")["status"], "running")

    def test_replay_rejects_conflict_invalid_ids_and_duplicate_start(self):
        with self.assertRaises(ValueError):
            self.server.replay_start(self.manager, ["missing"])
        with self.assertRaises(ValueError):
            self.server.replay_start(self.manager, [])
        self.server.set_session_control("live_generation", "running")
        with self.assertRaisesRegex(ValueError, "active playback"):
            self.server.replay_start(self.manager, ["original0"])
        self.server.set_session_control("live_generation", "stopped")
        self.server.replay_start(self.manager, ["original0"])
        with self.assertRaisesRegex(ValueError, "already active"):
            self.server.replay_start(self.manager, ["original0"])
        self.server.replay_control(self.manager, "stop")
        self.server.replay_start(self.manager, ["original0"])


if __name__ == "__main__":
    unittest.main()
