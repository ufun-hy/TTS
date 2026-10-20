"""Production-boundary regressions found while reviewing 2c6244f."""
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from audio_cache.manager import AudioCacheManager
from audio_cache.library import LibraryCapacityError
from audio_cache.server import AudioCacheServer, make_handler
from server.live_session import build_live_manager
from server.prepared_live_session import PreparedLiveSession


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\0\0" * 800)
    return output.getvalue()


class WindowsLiveReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tts-live-review-"))
        self.cache = AudioCacheManager(self.root / "cache")
        self.server = AudioCacheServer(("127.0.0.1", 0), make_handler(self.cache))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.manager = build_live_manager("http://unused", f"http://127.0.0.1:{self.server.server_port}")
        self.manager._synthesize_impl = lambda *_: wav()

    def tearDown(self):
        if self.manager._session:
            self.manager.stop()
            self.manager._session._thread.join(2)
        self.server.shutdown()
        self.server.server_close()

    def test_prepared_audio_uses_windows_data_directory(self):
        with patch.dict(os.environ, {"AI_LIVE_STUDIO_DATA": str(self.root)}):
            session = PreparedLiveSession("test", "default", [], lambda *_: wav(), lambda *_: None, lambda _: {})
        self.assertEqual(session.audio_root, self.root / "prepared-live" / "test")

    def test_pause_during_capacity_query_blocks_delivery(self):
        queried, release = threading.Event(), threading.Event()
        capacity = self.manager.library_capacity
        def slow_capacity():
            queried.set()
            release.wait(2)
            return capacity()
        self.manager.library_capacity = slow_capacity
        self.manager.start("default", [{"id": "p1", "text": "测试。"}], autoplay=False)
        try:
            self.assertTrue(queried.wait(1))
            self.manager.pause()
            release.set()
            time.sleep(.1)
            self.assertEqual(self.cache.library.capacity()["item_count"], 0)
            self.manager.resume()
            self.manager._session._thread.join(2)
            self.assertEqual(self.cache.library.capacity()["item_count"], 1)
        finally:
            release.set()

    def test_stop_after_successful_enqueue_counts_saved_inventory_and_preserves_playback(self):
        request = self.manager._request_json
        def stop_after_enqueue(method, path, *args, **kwargs):
            result = request(method, path, *args, **kwargs)
            if path == "/audio/enqueue":
                self.manager.stop()
            return result
        self.manager._request_json = stop_after_enqueue
        result = self.manager.start("default", [{"id": "p1", "text": "测试。"}], autoplay=False)
        self.manager._session._thread.join(2)
        self.assertEqual(self.manager.status()["generated_segments"], 1)
        self.assertEqual(self.cache.library.capacity()["item_count"], 1)
        self.assertEqual(self.server.session_control(result["session_id"])["status"], "paused")
        self.manager.playback_control("start")
        self.assertEqual(self.server.session_control(result["session_id"])["status"], "running")

    def test_capacity_race_retries_same_processed_wav_without_resynthesis(self):
        store = self.cache.library.store
        rejected = threading.Event()
        attempts = []
        def racing_store(item_id, path, metadata):
            attempts.append((item_id, path.read_bytes()))
            if len(attempts) == 1:
                rejected.set()
                raise LibraryCapacityError("disk became full")
            return store(item_id, path, metadata)
        with patch.object(self.cache.library, "store", side_effect=racing_store), \
                patch.object(self.cache, "_processor", wraps=self.cache._processor) as processor, \
                patch.object(self.manager, "_synthesize_impl", return_value=wav()) as synthesize:
            self.manager.start("default", [{"id": "p1", "text": "测试。"}], autoplay=False)
            self.assertTrue(rejected.wait(1))
            self.manager._session._thread.join(3)
            self.assertFalse(self.manager._session._thread.is_alive())
            synthesize.assert_called_once()
            processor.assert_called_once()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.manager.status()["generated_segments"], 1)
        item = self.cache.list_items("ready")[0]
        self.assertEqual(item.metadata["sequence"], 1)
        self.assertEqual(len(item.metadata["source_audio_sha256"]), 64)
        self.assertTrue(item.metadata["session_final"])
        self.assertEqual(self.cache.library.capacity()["item_count"], 1)


if __name__ == "__main__":
    unittest.main()
