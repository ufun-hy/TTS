"""Replay journal validation, atomic replacement and fail-safe restoration."""
import json
from pathlib import Path
import tempfile
import unittest

from audio_cache.replay_journal import ReplayJournal


class ReplayJournalTests(unittest.TestCase):
    def test_roundtrip_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ReplayJournal(Path(directory))
            sid = "replay_" + "a" * 16
            journal.save(sid, ["audio_1", "audio_2"], "running")
            self.assertEqual(journal.load()["item_ids"], ["audio_1", "audio_2"])
            journal.save(sid, ["audio_1", "audio_2"], "stopped")
            self.assertEqual(journal.load()["status"], "stopped")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_missing_or_damaged_record_never_grants_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ReplayJournal(Path(directory))
            self.assertEqual(journal.load(), {})
            journal.path.write_text("{invalid", encoding="utf-8")
            self.assertEqual(journal.load(), {})
            for invalid in (
                {"schema": 1, "session_id": "../bad", "item_ids": ["a"], "status": "running"},
                {"schema": 1, "session_id": "replay_" + "b"*16, "item_ids": [], "status": "running"},
                {"schema": 1, "session_id": "replay_" + "b"*16, "item_ids": ["../bad"], "status": "running"},
                {"schema": 1, "session_id": "replay_" + "b"*16, "item_ids": ["a"], "status": "unknown"},
            ):
                journal.path.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertEqual(journal.load(), {})


if __name__ == "__main__":
    unittest.main()
