import json
import math
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest

from audio_client.playback import PlaybackController, _playback_status


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


def _float_wav(samples=(0.0, 0.5, -0.5), rate=24000):
    frames = b"".join(struct.pack("<f", value) for value in samples)
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _write_failed_float_item(root: Path, item_id: str, sequence: int, session_id: str) -> None:
    (root / f"{item_id}.wav").write_bytes(_float_wav())
    (root / f"{item_id}.json").write_text(json.dumps({
        "status": "completed",
        "sequence": sequence,
        "playback_status": "playback_failed",
        "playback_error": "The specified file cannot be played",
        "downloaded_at": "2026-09-14T12:00:00+00:00",
        "server_metadata": {"session_id": session_id, "sequence": sequence},
    }), encoding="utf-8")


class PlaybackTests(unittest.TestCase):
    def test_explicit_buffering_is_not_legacy_cached(self):
        self.assertEqual(_playback_status({"status": "completed", "playback_status": "buffering"}), "buffering")
        self.assertEqual(_playback_status({"status": "completed"}), "cached")

    def test_startup_buffer_is_enforced_by_playback_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(root, "live_001", 1, "buffering", "session", "2026-09-17T00:00:01+00:00")
            metadata = json.loads((root / "live_001.json").read_text())
            metadata["duration"] = 5.0
            (root / "live_001.json").write_text(json.dumps(metadata), encoding="utf-8")
            player = FakePlayer(threading.Event())
            controller = PlaybackController(root, player=player)
            controller.start()
            time.sleep(0.25)
            self.assertEqual(player.ids, [])
            self.assertEqual(controller.stats()["playback_status"], "buffering")

            _write_item(root, "live_002", 2, "buffering", "session", "2026-09-17T00:00:02+00:00")
            metadata = json.loads((root / "live_002.json").read_text())
            metadata["duration"] = 7.0
            (root / "live_002.json").write_text(json.dumps(metadata), encoding="utf-8")
            deadline = time.time() + 2
            while len(player.ids) < 2 and time.time() < deadline:
                time.sleep(0.01)
            controller.stop()
            self.assertEqual(player.ids, ["live_001", "live_002"])

    def test_rebuffering_resumes_immediately_with_short_ready_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(root, "live_001", 1, "cached", "session", "2026-09-17T00:00:01+00:00")
            player = FakePlayer(threading.Event())
            controller = PlaybackController(root, player=player)
            controller.start()
            deadline = time.time() + 2
            while not player.ids and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(player.ids, ["live_001"])
            time.sleep(0.1)

            _write_item(root, "live_002", 2, "buffering", "session", "2026-09-17T00:00:02+00:00")
            metadata = json.loads((root / "live_002.json").read_text())
            metadata["duration"] = 3.0
            (root / "live_002.json").write_text(json.dumps(metadata), encoding="utf-8")
            deadline = time.monotonic() + 1
            while len(player.ids) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(player.ids, ["live_001", "live_002"])

            _write_item(root, "live_003", 3, "buffering", "session", "2026-09-17T00:00:03+00:00")
            metadata = json.loads((root / "live_003.json").read_text())
            metadata["duration"] = 9.0
            (root / "live_003.json").write_text(json.dumps(metadata), encoding="utf-8")
            deadline = time.time() + 2
            while len(player.ids) < 3 and time.time() < deadline:
                time.sleep(0.01)
            controller.stop()
            self.assertEqual(player.ids, ["live_001", "live_002", "live_003"])

    def test_final_short_item_force_releases_rebuffer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(root, "live_001", 1, "cached", "session", "2026-09-17T00:00:01+00:00")
            player = FakePlayer(threading.Event())
            controller = PlaybackController(root, player=player)
            controller.start()
            deadline = time.time() + 2
            while not player.ids and time.time() < deadline:
                time.sleep(0.01)
            metadata = json.loads((root / "live_001.json").read_text())
            metadata["server_metadata"]["session_final"] = False
            (root / "live_001.json").write_text(json.dumps(metadata), encoding="utf-8")
            _write_item(root, "live_002", 2, "buffering", "session", "2026-09-17T00:00:02+00:00")
            metadata = json.loads((root / "live_002.json").read_text())
            metadata["duration"] = 5.0
            metadata["server_metadata"]["session_final"] = True
            (root / "live_002.json").write_text(json.dumps(metadata), encoding="utf-8")
            deadline = time.time() + 2
            while len(player.ids) < 2 and time.time() < deadline:
                time.sleep(0.01)
            controller.stop()
            self.assertEqual(player.ids, ["live_001", "live_002"])
    def test_current_session_float_failure_is_converted_once_and_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_failed_float_item(root, "failed_float", 1, "session_current")
            controller = PlaybackController(root, player=FakePlayer(threading.Event()))
            controller._active_session_id = "session_current"
            controller._recover_failed_float_items()
            metadata_path = root / "failed_float.json"
            metadata = json.loads(metadata_path.read_text())
            self.assertEqual(metadata["playback_status"], "cached")
            self.assertEqual(metadata["wav_compat_recovery"]["attempt_count"], 1)
            self.assertTrue(metadata["wav_compat_recovery"]["success"])
            derived = list((root / "pcm16").glob("*.wav"))
            self.assertEqual(len(derived), 1)

            controller._recover_failed_float_items()
            self.assertEqual(len(list((root / "pcm16").glob("*.wav"))), 1)
            self.assertEqual(json.loads(metadata_path.read_text())["wav_compat_recovery"]["attempt_count"], 1)

    def test_failed_pcm16_and_other_sessions_are_not_compatibility_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_item(root, "failed_pcm", 1, playback_status="playback_failed", session_id="session_current", downloaded_at="2026-09-14T12:00:01+00:00")
            _write_failed_float_item(root, "failed_old_float", 2, "session_old")
            controller = PlaybackController(root, player=FakePlayer(threading.Event()))
            controller._active_session_id = "session_current"
            controller._sync_active_session = lambda _items: None
            controller._recover_failed_float_items()
            self.assertEqual(json.loads((root / "failed_pcm.json").read_text())["playback_status"], "playback_failed")
            self.assertEqual(json.loads((root / "failed_old_float.json").read_text())["playback_status"], "playback_failed")
            self.assertEqual(list((root / "pcm16").glob("*.wav")), [])

    def test_failed_float_conversion_is_recorded_once_and_not_retried_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_failed_float_item(root, "bad_float", 1, "session_current")
            (root / "bad_float.wav").write_bytes(_float_wav((math.nan,)))
            controller = PlaybackController(root, player=FakePlayer(threading.Event()))
            controller._active_session_id = "session_current"
            controller._recover_failed_float_items()
            controller._recover_failed_float_items()
            metadata = json.loads((root / "bad_float.json").read_text())
            self.assertEqual(metadata["playback_status"], "playback_failed")
            self.assertEqual(metadata["wav_compat_recovery"]["attempt_count"], 1)
            self.assertFalse(metadata["wav_compat_recovery"]["success"])
            self.assertEqual(list((root / "pcm16").glob("*.wav")), [])

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
