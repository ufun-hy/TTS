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

    def play(self, path, speed, volume, stop_event, pause_event):
        self.ids.append(path.stem)
        self.done.wait(0.01)


class PlaybackTests(unittest.TestCase):
    def test_sequence_order_and_played_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item_id, sequence in (("segment_002", 2), ("segment_001", 1), ("segment_003", 3)):
                (root / f"{item_id}.wav").write_bytes(b"RIFF")
                (root / f"{item_id}.json").write_text(json.dumps({
                    "status": "completed",
                    "sequence": sequence,
                    "playback_status": "cached",
                }), encoding="utf-8")
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
            (root / "segment_001.wav").write_bytes(b"RIFF")
            metadata = root / "segment_001.json"
            metadata.write_text(json.dumps({"status": "completed", "sequence": 1, "playback_status": "playing"}), encoding="utf-8")
            controller = PlaybackController(root, player=FakePlayer(threading.Event()))
            self.assertEqual(json.loads(metadata.read_text())["playback_status"], "cached")
            controller.stop()


if __name__ == "__main__":
    unittest.main()
