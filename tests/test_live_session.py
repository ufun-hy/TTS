import time
import unittest

from server.live_session import LiveSessionError, LiveSessionManager, prepare_live_segments


class LiveSessionTests(unittest.TestCase):
    def test_prepare_segments_keeps_paragraph_order_without_resegmentation(self):
        segments = prepare_live_segments([
            {"id": "p0001", "text": "第一段确认文本"},
            {"id": "p0002", "text": "第二段确认文本"},
        ])
        self.assertEqual(segments, [
            {"id": "p0001", "text": "第一段确认文本"},
            {"id": "p0002", "text": "第二段确认文本"},
        ])

    def test_prepare_segments_only_chunks_oversized_text(self):
        segments = prepare_live_segments([{"id": "p0005", "text": "甲" * 450}])
        self.assertEqual([segment["id"] for segment in segments], ["p0005-01", "p0005-02", "p0005-03"])
        self.assertEqual([len(segment["text"]) for segment in segments], [200, 200, 50])
        self.assertEqual("".join(segment["text"] for segment in segments), "甲" * 450)

    def test_start_rejects_missing_confirmed_segments(self):
        manager = LiveSessionManager("http://gateway", "http://cache", synthesize=lambda *_args: b"RIFF")
        with self.assertRaises(LiveSessionError) as error:
            manager.start("default", None)
        self.assertEqual(error.exception.status, 400)
        self.assertIn("segments", str(error.exception))

    def test_start_rejects_invalid_audio_settings(self):
        manager = LiveSessionManager("http://gateway", "http://cache", synthesize=lambda *_args: b"RIFF")
        with self.assertRaises(LiveSessionError) as error:
            manager.start("default", [{"id": "p0001", "text": "测试"}], playback_speed=0)
        self.assertEqual(error.exception.status, 400)

    def test_audio_settings_are_sent_to_audio_cache(self):
        requests = []
        manager = LiveSessionManager("http://gateway", "http://cache", synthesize=lambda *_args: b"RIFF")
        manager._request_json = lambda method, path, body=None, cache=False: requests.append((method, path, body, cache)) or {}
        manager.start("default", [{"id": "p0001", "text": "测试"}], playback_speed=1.05, volume=80)
        deadline = time.monotonic() + 1
        while manager.status()["status"] in ("starting", "running"):
            if time.monotonic() >= deadline:
                self.fail("live session did not finish")
            time.sleep(0.01)
        enqueue = next(item for item in requests if item[1] == "/audio/enqueue")
        self.assertEqual(enqueue[2]["playback_speed"], 1.05)
        self.assertEqual(enqueue[2]["volume"], 80.0)

    def test_session_generates_and_enqueues_each_segment(self):
        enqueued = []

        def enqueue(item_id, sequence, text, voice, audio):
            enqueued.append((item_id, sequence, text, voice, audio))

        manager = LiveSessionManager(
            "http://gateway",
            "http://cache",
            synthesize=lambda text, voice: b"RIFF" + text.encode(),
            enqueue=enqueue,
            cache_status=lambda _session_id: {"ready": len(enqueued), "client_connected": True},
            cache_cleanup=lambda _session_id: {},
        )

        self.assertEqual(manager.start("default", [
            {"id": "p0001", "text": "第一段确认文本"},
            {"id": "p0002", "text": "第二段确认文本"},
        ])["status"], "starting")
        deadline = time.monotonic() + 1
        while manager.status()["status"] == "starting" or manager.status()["status"] == "running":
            if time.monotonic() >= deadline:
                self.fail("live session did not finish")
            time.sleep(0.01)
        status = manager.status()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["generated_segments"], 2)
        self.assertEqual(status["ready_segments"], 2)
        self.assertEqual(status["processing_segments"], 0)
        self.assertEqual(status["completed_segments"], 0)
        self.assertTrue(status["client_connected"])
        self.assertEqual([item[0] for item in enqueued], ["p0001", "p0002"])
        self.assertEqual([item[2] for item in enqueued], ["第一段确认文本", "第二段确认文本"])

        with self.assertRaises(LiveSessionError):
            manager.start("default", [{"id": "p0001", "text": "不能重复启动"}])
        manager.reset()

    def test_failed_segment_sets_failed_state(self):
        manager = LiveSessionManager(
            "http://gateway",
            "http://cache",
            synthesize=lambda _text, _voice: (_ for _ in ()).throw(RuntimeError("tts down")),
            enqueue=lambda *_args: None,
            cache_status=lambda _session_id: {},
            cache_cleanup=lambda _session_id: {},
        )
        manager.start("default", [{"id": "p0001", "text": "测试文本"}])
        deadline = time.monotonic() + 1
        while manager.status()["status"] == "starting":
            if time.monotonic() >= deadline:
                self.fail("live session did not fail")
            time.sleep(0.01)
        self.assertEqual(manager.status()["status"], "failed")
        self.assertIn("tts down", manager.status()["error"])


if __name__ == "__main__":
    unittest.main()
