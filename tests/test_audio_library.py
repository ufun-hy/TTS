"""Durable generated audio is not removed by delivery ACK or session cleanup."""
import tempfile
import unittest
from pathlib import Path
from audio_cache.manager import AudioCacheManager


class AudioLibraryTests(unittest.TestCase):
    @staticmethod
    def manager(root):
        return AudioCacheManager(root, processor=lambda data, meta: (data, {"duration": 1.25}))

    def test_archive_survives_ack_cleanup_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.manager(root)
            manager.add_audio(
                b"RIFF_fake_wav_test", {
                    "source": "live_session", "session_id": "session1",
                    "sequence": 1, "voice": "test",
                }, "testitem1"
            )
            items = manager.library.list("session1")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["duration"], 1.25)
            manager.claim_next("session1")
            manager.ack("testitem1")
            manager.cleanup_session("session1")
            self.assertEqual(len(manager.library.list("session1")), 1)
            self.assertEqual(manager.library.file("testitem1").read_bytes(), b"RIFF_fake_wav_test")
            restarted = self.manager(root)
            self.assertEqual(len(restarted.library.list("session1")), 1)

    def test_only_generated_live_audio_is_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(Path(directory))
            manager.add_audio(b"RIFF", {"session_id": "s1"}, "other1")
            self.assertEqual(manager.library.list(), [])

    def test_listing_is_bounded_and_id_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(Path(directory))
            manager.add_audio(b"RIFF", {"source": "live_session", "session_id": "s1"}, "abc1")
            self.assertEqual(len(manager.library.list(limit=1)), 1)
            with self.assertRaises(ValueError):
                manager.library.file("../bad")


if __name__ == "__main__":
    unittest.main()
