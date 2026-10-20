from datetime import datetime, timedelta, timezone
import threading
import time
import unittest
from unittest.mock import patch

from server.live_session import (
    LiveSession,
    LiveSessionError,
    LiveSessionManager,
    CACHE_ENQUEUE_TIMEOUT_SECONDS,
    choose_candidate_round,
    prepare_candidate_pools,
    prepare_live_segments,
    resolve_dynamic_time,
)
from server.tts_client import RetryableTTSClientError


class _RepeatRng:
    def randrange(self, start, stop=None):
        return 0 if stop is None else start

    def choice(self, values):
        return values[0]


class LiveSessionTests(unittest.TestCase):
    def test_dynamic_time_uses_the_generation_clock(self):
        now = datetime(2026, 9, 14, 14, 46, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(
            resolve_dynamic_time(
                "现在是{{current_time}}，日期{{current_date}}，{{current_weekday}}。",
                now,
            ),
            "现在是下午2点46分，日期2026年9月14日，星期一。",
        )
        self.assertEqual(resolve_dynamic_time("未知{{token}}", now), "未知{{token}}")

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

    def test_candidate_round_keeps_paragraph_order_and_changes_repeated_round(self):
        pools = prepare_candidate_pools([
            {"id": "p0001", "candidates": ["A1", "A2"]},
            {"id": "p0002", "candidates": ["B1", "B2"]},
        ])
        segments, indexes = choose_candidate_round(pools, [0, 0], _RepeatRng())
        self.assertEqual([item["id"] for item in segments], ["p0001", "p0002"])
        self.assertNotEqual(indexes, [0, 0])
        self.assertIn(segments[0]["text"], ("A1", "A2"))
        self.assertIn(segments[1]["text"], ("B1", "B2"))

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
        manager._request_json = lambda method, path, body=None, cache=False, timeout=None: requests.append((method, path, body, cache, timeout)) or {}
        manager.start("default", [{"id": "p0001", "text": "测试"}], playback_speed=1.05, volume=80)
        deadline = time.monotonic() + 1
        while manager.status()["status"] in ("starting", "running"):
            if time.monotonic() >= deadline:
                self.fail("live session did not finish")
            time.sleep(0.01)
        enqueue = next(item for item in requests if item[1] == "/audio/enqueue")
        self.assertEqual(enqueue[2]["playback_speed"], 1.05)
        self.assertEqual(enqueue[2]["volume"], 80.0)
        self.assertEqual(enqueue[4], CACHE_ENQUEUE_TIMEOUT_SECONDS)

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
        while manager.status()["status"] in ("starting", "running"):
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
        self.assertEqual([item[1] for item in enqueued], [1, 2])
        self.assertEqual([item[2] for item in enqueued], ["第一段确认文本", "第二段确认文本"])
        self.assertEqual(len({item[0] for item in enqueued}), 2)

        with self.assertRaises(LiveSessionError):
            manager.start("default", [{"id": "p0001", "text": "不能重复启动"}])
        manager.reset()

    def test_loop_generates_new_random_rounds_with_monotonic_sequence(self):
        enqueued = []
        holder = {}

        def enqueue(item_id, sequence, text, voice, audio):
            enqueued.append((item_id, sequence, text, voice, audio))
            if len(enqueued) >= 4:
                holder["manager"].stop()

        manager = LiveSessionManager(
            "http://gateway",
            "http://cache",
            synthesize=lambda text, voice: b"RIFF" + text.encode(),
            enqueue=enqueue,
            cache_status=lambda _session_id: {"ready": len(enqueued), "client_connected": True},
            cache_cleanup=lambda _session_id: {},
        )
        holder["manager"] = manager
        result = manager.start("default", [
            {"id": "p0001", "candidates": ["A1", "A2"]},
            {"id": "p0002", "candidates": ["B1", "B2"]},
        ])
        self.assertTrue(result["looping"])

        deadline = time.monotonic() + 1
        while len(enqueued) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(enqueued), 4)
        self.assertEqual([item[1] for item in enqueued], [1, 2, 3, 4])
        self.assertNotEqual(
            tuple(item[2] for item in enqueued[:2]),
            tuple(item[2] for item in enqueued[2:4]),
        )
        self.assertEqual(len({item[0] for item in enqueued}), 4)
        self.assertEqual(manager.status()["round_number"], 2)

    def test_high_buffer_does_not_pause_generation(self):
        synthesized = []
        enqueued = []
        session_id = "live-backpressure"

        def cache_status(_session_id):
            return {
                "ready": 0,
                "processing": 0,
                "completed": 0,
                "failed": 0,
                "client_connected": True,
                "client_state": {
                    "session_id": session_id,
                    "buffered_segments": 3,
                    "buffered_seconds": 0.0,
                    "playback_status": "buffered",
                },
            }

        def synthesize(text, _voice):
            synthesized.append(text)
            return b"RIFF" + text.encode()

        def enqueue(item_id, sequence, text, voice, audio):
            enqueued.append((item_id, sequence, text, voice, audio))
            if len(enqueued) == 1:
                cache_status_state["buffered_seconds"] = 300.0

        cache_status_state = {"buffered_seconds": 0.0}

        def dynamic_cache_status(_session_id):
            value = cache_status(_session_id)
            value["client_state"]["buffered_seconds"] = cache_status_state["buffered_seconds"]
            return value

        session = LiveSession(
            session_id,
            "default",
            [{"id": "p1", "text": "第一段"}, {"id": "p2", "text": "第二段"}],
            synthesize,
            enqueue,
            dynamic_cache_status,
            buffer_high_seconds=30.0,
            buffer_low_seconds=12.0,
        )
        session.start()
        deadline = time.monotonic() + 1
        while len(enqueued) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(enqueued), 1)
        cache_status_state["buffered_seconds"] = 300.0
        session._thread.join(timeout=1)
        self.assertEqual(len(enqueued), 2)
        self.assertEqual(synthesized, ["第一段", "第二段"])
        self.assertFalse(session._thread.is_alive())
        self.assertFalse(session.snapshot().backpressure_active)
        self.assertEqual(session.status, "stopped")

    def test_inventory_capacity_waits_then_resumes_without_buffer_ack(self):
        synthesized, enqueued = [], []
        limit = 100 * 1024**3
        capacity = {
            "used_bytes": limit,
            "limit_bytes": limit,
            "remaining_bytes": 0,
            "disk_free_bytes": 1_000_000_000,
            "metadata_reserve_bytes": 16_384,
            "free_space_reserve_bytes": 16 * 1024 * 1024,
            "max_audio_expansion_ratio": 1.1,
        }

        def request(method, path, body=None, cache=False, timeout=None):
            if path == "/audio/library/capacity":
                return dict(capacity)
            if path == "/audio/enqueue":
                enqueued.append(body)
                return {}
            return {}

        manager = LiveSessionManager(
            "http://gateway", "http://cache",
            synthesize=lambda text, _voice: synthesized.append(text) or b"RIFFaudio",
            cache_status=lambda session_id: {
                "client_connected": False,
                "client_state": {"session_id": session_id, "buffered_seconds": 300},
            },
            capacity_guard=True,
        )
        manager._request_json = request
        manager.start("default", [{"id": "p1", "text": "第一段"}])

        deadline = time.monotonic() + 1
        while not manager.status()["capacity_waiting"] and time.monotonic() < deadline:
            time.sleep(0.01)
        waiting = manager.status()
        self.assertTrue(waiting["capacity_waiting"])
        self.assertEqual(waiting["capacity_reason"], "inventory")
        self.assertIn("100.00 / 100.00 GiB", waiting["error"])
        self.assertEqual(synthesized, ["第一段"])
        self.assertEqual(enqueued, [])

        capacity.update(used_bytes=0, remaining_bytes=limit)
        manager._session._thread.join(timeout=2)
        self.assertFalse(manager._session._thread.is_alive())
        self.assertEqual(manager.status()["status"], "stopped")
        self.assertFalse(manager.status()["capacity_waiting"])
        self.assertEqual([item["sequence"] for item in enqueued], [1])
        self.assertEqual(enqueued[0]["text"], "第一段")

    def test_stop_interrupts_inventory_capacity_wait(self):
        enqueued = []
        limit = 100 * 1024**3
        manager = LiveSessionManager(
            "http://gateway", "http://cache", synthesize=lambda *_: b"RIFFaudio",
            cache_status=lambda session_id: {"client_state": {"session_id": session_id}},
            capacity_guard=True,
        )
        manager._request_json = lambda method, path, body=None, cache=False, timeout=None: {
            "used_bytes": limit, "limit_bytes": limit, "remaining_bytes": 0,
            "disk_free_bytes": 1_000_000_000, "metadata_reserve_bytes": 16_384,
            "free_space_reserve_bytes": 16 * 1024 * 1024, "max_audio_expansion_ratio": 1.1,
        } if path == "/audio/library/capacity" else enqueued.append(body) or {}
        manager.start("default", [{"id": "p1", "text": "第一段"}])

        deadline = time.monotonic() + 1
        while not manager.status()["capacity_waiting"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(manager.status()["capacity_waiting"])
        manager.stop()
        manager._session._thread.join(timeout=0.5)
        self.assertFalse(manager._session._thread.is_alive())
        self.assertEqual(manager.status()["status"], "stopped")
        self.assertEqual(enqueued, [])

    def test_temporary_tts_retries_are_bounded(self):
        attempts = []

        def synthesize(*_args):
            attempts.append(True)
            raise RetryableTTSClientError("temporarily unavailable", status_code=503)

        manager = LiveSessionManager(
            "http://gateway", "http://cache", synthesize=synthesize,
            enqueue=lambda *_: None, cache_status=lambda _session_id: {},
        )
        with patch.object(RetryableTTSClientError, "retry_delay", return_value=0.001):
            manager.start("default", [{"id": "p1", "text": "第一段"}])
            manager._session._thread.join(timeout=1)

        self.assertFalse(manager._session._thread.is_alive())
        self.assertEqual(manager.status()["status"], "failed")
        self.assertEqual(len(attempts), 6)

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

    def test_stop_reports_stopping_until_worker_exits(self):
        started = threading.Event()
        release = threading.Event()
        session = LiveSession(
            "stop-state",
            "default",
            [{"id": "p1", "text": "测试文本"}],
            lambda _text, _voice: (started.set(), release.wait(2), b"RIFF")[2],
            lambda *_args: None,
            lambda _session_id: {"client_connected": True},
            stop_timeout_seconds=1,
        )
        session.start()
        self.assertTrue(started.wait(1))
        session.stop()
        self.assertEqual(session.status, "stopping")
        self.assertFalse(session.snapshot().as_dict()["stop_timed_out"])
        release.set()
        session._thread.join(2)
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(session.status, "stopped")

    def test_stop_timeout_is_reported_without_claiming_completion(self):
        started = threading.Event()
        release = threading.Event()
        session = LiveSession(
            "stop-timeout",
            "default",
            [{"id": "p1", "text": "测试文本"}],
            lambda _text, _voice: (started.set(), release.wait(2), b"RIFF")[2],
            lambda *_args: None,
            lambda _session_id: {"client_connected": True},
            stop_timeout_seconds=0.01,
        )
        session.start()
        self.assertTrue(started.wait(1))
        session.stop()
        time.sleep(0.03)
        status = session.snapshot().as_dict()
        self.assertEqual(status["status"], "stopping")
        self.assertTrue(status["stop_timed_out"])
        release.set()
        session._thread.join(2)
        self.assertEqual(session.status, "stopped")


if __name__ == "__main__":
    unittest.main()
