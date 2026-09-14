import time
import unittest

from server.live_session_blocks import (
    MAX_SYNTHESIS_BLOCK_CHARS,
    SynthesisBlockLiveSession,
    SynthesisBlockLiveSessionManager,
    prepare_synthesis_blocks,
)


class LiveSynthesisBlockTests(unittest.TestCase):
    def test_merges_adjacent_short_segments_with_paragraph_pause(self):
        blocks = prepare_synthesis_blocks([
            {"id": "p0001", "text": "第一段确认文本"},
            {"id": "p0002", "text": "第二段确认文本"},
        ])
        self.assertEqual(blocks, [
            {"id": "p0001", "text": "第一段确认文本\n第二段确认文本"},
        ])

    def test_starts_new_block_when_target_would_be_exceeded(self):
        first = "甲" * 100
        second = "乙" * 80
        blocks = prepare_synthesis_blocks([
            {"id": "p0001", "text": first},
            {"id": "p0002", "text": second},
        ])
        self.assertEqual(len(blocks), 2)
        self.assertEqual([block["text"] for block in blocks], [first, second])
        self.assertEqual(MAX_SYNTHESIS_BLOCK_CHARS, 180)

    def test_keeps_single_technical_chunk_intact_even_above_target(self):
        long_chunk = "甲" * 200
        blocks = prepare_synthesis_blocks([
            {"id": "p0001-01", "text": long_chunk},
            {"id": "p0002", "text": "下一段"},
        ])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["text"], long_chunk)
        self.assertEqual(blocks[1]["text"], "下一段")

    def test_connected_client_without_current_session_state_releases_backpressure(self):
        session = SynthesisBlockLiveSession(
            "live-recover",
            "default",
            [],
            lambda _text, _voice: b"RIFF",
            lambda *_args: None,
            lambda _session_id: {
                "client_connected": True,
                "client_state": {},
            },
            buffer_high_seconds=30.0,
            buffer_low_seconds=12.0,
        )
        session._client_session_seen = True
        session._backpressure_active = True

        session._wait_for_buffer_capacity()

        self.assertFalse(session._backpressure_active)

    def test_live_session_synthesizes_two_short_segments_once(self):
        synthesized = []
        enqueued = []

        def synthesize(text, voice):
            synthesized.append((text, voice))
            return b"RIFF" + text.encode()

        def enqueue(item_id, sequence, text, voice, audio):
            enqueued.append((item_id, sequence, text, voice, audio))

        manager = SynthesisBlockLiveSessionManager(
            "http://gateway",
            "http://cache",
            synthesize=synthesize,
            enqueue=enqueue,
            cache_status=lambda _session_id: {
                "ready": len(enqueued),
                "processing": 0,
                "completed": 0,
                "failed": 0,
                "client_connected": True,
            },
            cache_cleanup=lambda _session_id: {},
        )

        result = manager.start("default", [
            {"id": "p0001", "text": "第一段确认文本"},
            {"id": "p0002", "text": "第二段确认文本"},
        ])
        self.assertEqual(result["status"], "starting")

        deadline = time.monotonic() + 1
        while manager.status()["status"] in ("starting", "running"):
            if time.monotonic() >= deadline:
                self.fail("live session did not finish")
            time.sleep(0.01)

        expected_text = "第一段确认文本\n第二段确认文本"
        self.assertEqual(synthesized, [(expected_text, "default")])
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0][1], 1)
        self.assertEqual(enqueued[0][2], expected_text)
        status = manager.status()
        self.assertEqual(status["generated_segments"], 1)
        self.assertEqual(status["total_segments"], 1)
        self.assertEqual(status["ready_segments"], 1)

    def test_live_session_resolves_dynamic_time_before_synthesis_and_enqueue(self):
        synthesized = []
        enqueued = []

        manager = SynthesisBlockLiveSessionManager(
            "http://gateway",
            "http://cache",
            synthesize=lambda text, voice: synthesized.append((text, voice)) or b"RIFF",
            enqueue=lambda item_id, sequence, text, voice, audio: enqueued.append(text),
            cache_status=lambda _session_id: {"ready": len(enqueued), "client_connected": True},
            cache_cleanup=lambda _session_id: {},
        )
        manager.start("default", [{"id": "p0001", "text": "现在是{{current_time}}。"}])

        deadline = time.monotonic() + 1
        while manager.status()["status"] in ("starting", "running"):
            if time.monotonic() >= deadline:
                self.fail("live session did not finish")
            time.sleep(0.01)

        self.assertEqual(manager.status()["status"], "stopped")
        self.assertEqual(len(synthesized), 1)
        self.assertNotIn("{{current_time}}", synthesized[0][0])
        self.assertEqual(enqueued, [synthesized[0][0]])

    def test_loop_mode_batches_each_round_into_one_synthesis_start(self):
        enqueued = []
        holder = {}

        def enqueue(item_id, sequence, text, voice, audio):
            enqueued.append((item_id, sequence, text, voice, audio))
            if len(enqueued) >= 2:
                holder["manager"].stop()

        manager = SynthesisBlockLiveSessionManager(
            "http://gateway",
            "http://cache",
            synthesize=lambda text, voice: b"RIFF" + text.encode(),
            enqueue=enqueue,
            cache_status=lambda _session_id: {
                "ready": len(enqueued),
                "processing": 0,
                "completed": 0,
                "failed": 0,
                "client_connected": True,
            },
            cache_cleanup=lambda _session_id: {},
        )
        holder["manager"] = manager
        result = manager.start("default", [
            {"id": "p0001", "candidates": ["A1", "A2"]},
            {"id": "p0002", "candidates": ["B1", "B2"]},
        ])
        self.assertTrue(result["looping"])

        deadline = time.monotonic() + 1
        while len(enqueued) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(enqueued), 2)
        self.assertEqual([item[1] for item in enqueued], [1, 2])
        for item in enqueued:
            self.assertIn("\n", item[2])
        self.assertNotEqual(enqueued[0][2], enqueued[1][2])


if __name__ == "__main__":
    unittest.main()
