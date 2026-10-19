"""Capacity limits and deletion protections for the durable WAV library."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audio_cache.manager import AudioCacheManager
from audio_cache.processing import ProcessingResult
from audio_cache.server import AudioCacheServer


class LibraryCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = AudioCacheManager(
            self.root, processor=lambda audio, metadata: ProcessingResult(audio, raw_duration=1.0, duration=1.0)
        )
        self.manager.add_audio(b"RIFF1234", {"source": "live_session", "session_id": "s"}, "one")
        self.manager.add_audio(b"RIFF5678", {"source": "live_session", "session_id": "s"}, "two")

    def tearDown(self):
        self.temp.cleanup()

    def test_reports_total_across_all_inventory(self):
        stats = self.manager.library.capacity()
        self.assertEqual(stats["item_count"], 2)
        self.assertGreater(stats["used_bytes"], 16)
        self.assertGreater(stats["remaining_bytes"], 0)

    def test_rejects_overquota_without_erasing_existing_archive(self):
        with patch.dict(os.environ, {"AI_LIVE_STUDIO_LIBRARY_MAX_GIB": "0.01"}):
            huge = self.root / "large.wav"
            huge.write_bytes(b"x" * (11 * 1024 * 1024))
            with self.assertRaisesRegex(ValueError, "capacity reached"):
                self.manager.library.store("too_big", huge, {})
        self.assertEqual(self.manager.library.capacity()["item_count"], 2)
        self.assertEqual(self.manager.library.file("one").read_bytes(), b"RIFF1234")

    def test_explicit_deletion_and_protected_ids(self):
        with self.assertRaisesRegex(ValueError, "active playback"):
            self.manager.library.delete(["one"], {"one"})
        with self.assertRaises(ValueError):
            self.manager.library.delete(["one", "one"], set())
        with self.assertRaises(ValueError):
            self.manager.library.delete(["one", "missing"], set())
        self.assertEqual(self.manager.library.capacity()["item_count"], 2)
        result = self.manager.library.delete(["one"], set())
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(self.manager.library.get("one"))
        self.assertTrue(self.manager.library.file("two").is_file())

    def test_active_replay_source_can_be_identified_for_protection(self):
        server = AudioCacheServer(("127.0.0.1", 0), lambda *args, **kwargs: None)
        try:
            reply = server.replay_start(self.manager, ["one"])
            self.assertEqual(reply["status"], "running")
            self.assertIn("one", server.replay_status(self.manager)["item_ids"])
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
