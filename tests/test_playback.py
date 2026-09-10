import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from audio_client.playback import PlaybackController


class FakePlayer:
    def __init__(self, done):
        self.done = done
        self.ids = []

    def play(self, path, stop_event, pause_event):
        self.ids.append(path.stem)
        self.done.wait(0.01)


def _write_item(
    root: Path,
    item_id: str,
    sequence: int,
    playback_status: str = "cached",
    session_id: str = "",
    downloaded_at: str = "",
) -> None:
    (root / f"{item_id}.wav").write_bytes(b"RIFF")
    metadata = {
        "status": "completed",
        "sequence": sequence,
        "playback_status": playback_status,
    }
    if session_id:
        metadata["server_metadata"] = {"session_id": session_id, "sequence": sequence}
    if downloaded_at:
        metadata["downloaded_at"] = downloaded_at
    (root / f"{item_id}.json").write_text(json.dumps(metadata), encoding="utf-8")


class PlaybackTests(unittest.TestCase):
    def test_sequence_order_and_played_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item_id, sequence in (("segment_002", 2), ("segment_001", 1), ("segment_003", 3)):
                _write_item(root, item_id, sequence)
            done = threading.Event()
            player = FakePlayer(done)
            controller = PlaybackController(root, player=player)
            controller.start()
            deadline = time.time() + 2
            while len(player.ids) < 3 and time.time() < deadline:
                time.sleep(0.01)
            done.set()
            controller.stop()
            self.assertEqual(player.ids, ["segment_001", "segment_002", "segment_003"])
            self.assertEqual(controller.stats()["played"], 3)

    def test_interrupted_playback_returns_to_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(root, "segment_001", 1, playback_status="playing")
            metadata = root / "segment_001.json"
            controller = PlaybackController(root, player=FakePlayer(threading.Event()))
            self.assertEqual(json.loads(metadata.read_text())["playback_status"], "cached")
            controller.stop()

    def test_new_live_session_removes_safe_old_session_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(
                root,
                "old_played",
                1,
                playback_status="played",
                session_id="session_old",
                downloaded_at="2026-09-10T08:00:00+00:00",
            )
            _write_item(
                root,
                "old_cached",
                2,
                session_id="session_old",
                downloaded_at="2026-09-10T08:00:01+00:00",
            )
            _write_item(
                root,
                "new_001",
                1,
                session_id="session_new",
                downloaded_at="2026-09-10T08:10:00+00:00",
            )
            _write_item(
                root,
                "new_002",
                2,
                session_id="session_new",
                downloaded_at="2026-09-10T08:10:01+00:00",
            )

            player = FakePlayer(threading.Event())
            controller = PlaybackController(root, player=player)
            controller.start()
            deadline = time.time() + 2
            while len(player.ids) < 2 and time.time() < deadline:
                time.sleep(0.01)
            controller.stop()

            self.assertEqual(player.ids, ["new_001", "new_002"])
            self.assertFalse((root / "old_cached.wav").exists())
            self.assertFalse((root / "old_cached.json").exists())
            self.assertFalse((root / "old_played.wav").exists())
            self.assertFalse((root / "old_played.json").exists())
            stats = controller.stats()
            self.assertEqual(stats["active_session_id"], "session_new")
            self.assertEqual(stats["buffered_segments"], 0)

    def test_manual_clear_cache_only_removes_safe_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(root, "played", 1, playback_status="played")
            _write_item(root, "superseded", 2, playback_status="superseded")
            _write_item(root, "failed", 3, playback_status="playback_failed")
            _write_item(root, "cached", 4, playback_status="cached")
            _write_item(root, "playing", 5, playback_status="playing")

            controller = PlaybackController(root, player=FakePlayer(threading.Event()))
            # Interrupted "playing" entries are recovered to cached on startup.
            result = controller.clear_cache()

            self.assertEqual(result["removed_items"], 3)
            for item_id in ("played", "superseded", "failed"):
                self.assertFalse((root / f"{item_id}.wav").exists())
                self.assertFalse((root / f"{item_id}.json").exists())
            for item_id in ("cached", "playing"):
                self.assertTrue((root / f"{item_id}.wav").exists())
                self.assertTrue((root / f"{item_id}.json").exists())

    def test_legacy_non_session_items_keep_existing_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(root, "legacy_002", 2)
            _write_item(root, "legacy_001", 1)
            player = FakePlayer(threading.Event())
            controller = PlaybackController(root, player=player)
            controller.start()
            deadline = time.time() + 2
            while len(player.ids) < 2 and time.time() < deadline:
                time.sleep(0.01)
            controller.stop()
            self.assertEqual(player.ids, ["legacy_001", "legacy_002"])


if __name__ == "__main__":
    unittest.main()
