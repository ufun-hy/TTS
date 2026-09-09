import time
import unittest

from server.live_session import LiveSessionError, LiveSessionManager, split_live_text


class LiveSessionTests(unittest.TestCase):
    def test_split_live_text_keeps_sentence_order(self):
        self.assertEqual(split_live_text("第一句。第二句？\n\n第三段！"), ["第一句。", "第二句？", "第三段！"])

    def test_split_live_text_chunks_long_sentences_for_gateway_limit(self):
        segments = split_live_text("甲" * 450)
        self.assertEqual([len(segment) for segment in segments], [200, 200, 50])

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

        self.assertEqual(manager.start("default", "第一句。第二句。")["status"], "starting")
        deadline = time.monotonic() + 1
        while manager.status()["status"] == "starting" or manager.status()["status"] == "running":
            if time.monotonic() >= deadline:
                self.fail("live session did not finish")
            time.sleep(0.01)
        status = manager.status()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["generated_segments"], 2)
        self.assertEqual(status["cache_ready"], 2)
        self.assertTrue(status["client_connected"])
        self.assertEqual([item[0] for item in enqueued], ["segment_001", "segment_002"])

        with self.assertRaises(LiveSessionError):
            manager.start("default", "不能重复启动。")
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
        manager.start("default", "测试。")
        deadline = time.monotonic() + 1
        while manager.status()["status"] == "starting":
            if time.monotonic() >= deadline:
                self.fail("live session did not fail")
            time.sleep(0.01)
        self.assertEqual(manager.status()["status"], "failed")
        self.assertIn("tts down", manager.status()["error"])


if __name__ == "__main__":
    unittest.main()
