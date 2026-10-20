from pathlib import Path
import io
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from server.live_session import build_live_manager
from server.live_session_blocks import SynthesisBlockLiveSessionManager
from server.prepared_live_session import PreparedLiveSession
from timeline.tts_client import RetryableTTSClientError
from audio_cache.manager import AudioCacheManager
from audio_cache.server import AudioCacheServer, make_handler
from audio_client.client import AudioClient
from audio_client.playback import PlaybackController


class PreparedLiveTests(unittest.TestCase):
    def make_session(self, synthesize, enqueue, cache_status=lambda _: {}, **kwargs):
        session = PreparedLiveSession(
            "prepared-test", "default", [], synthesize, enqueue, cache_status,
            candidate_pools=[{"id": "p1", "candidates": ["A", "B"]}],
            audio_root=Path(tempfile.mkdtemp(prefix="tts-prepared-test-")), **kwargs,
        )
        self.addCleanup(self.stop, session)
        return session

    @staticmethod
    def stop(session):
        session.stop()
        if session._thread.ident:
            session._thread.join(2)

    def test_first_round_ready_then_new_random_rounds_continue(self):
        calls, sent = [], []

        def synthesize(text, _voice):
            calls.append(text)
            return b"RIFF" + text.encode()

        def enqueue(*args):
            self.assertEqual(session.prepared_rounds, 1)
            self.assertEqual(session.prepared_segments, session.preparation_total_segments)
            self.assertEqual(len(calls), len(sent) + 1)
            sent.append(args)
            if len(sent) == 10:
                session.stop()

        session = self.make_session(synthesize, enqueue,
            cache_status=lambda _: {"completed": len(sent)})
        session.start()
        session._thread.join(2)
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(len(sent), 10)
        self.assertEqual([item[1] for item in sent], list(range(1, 11)))
        self.assertEqual(len({item[0] for item in sent}), 10)
        self.assertTrue(all(a[2] != b[2] for a, b in zip(sent, sent[1:])))
        self.assertTrue(all(item[4] == b"RIFF" + item[2].encode() for item in sent))
        self.assertEqual(session.snapshot().as_dict()["prepared_rounds"], 1)

    def test_preparation_ignores_windows_backpressure_and_pause_resume_still_gates_next_call(self):
        first, release, second = threading.Event(), threading.Event(), threading.Event()
        calls, sent = [], []

        def synthesize(text, _voice):
            calls.append(text)
            if len(calls) == 1:
                first.set()
                release.wait(2)
            else:
                second.set()
            return b"RIFF"

        session = self.make_session(synthesize, lambda *args: (sent.append(args), session.stop()),
            cache_status=lambda sid: {"client_connected": False,
                "client_state": {"session_id": sid, "buffered_seconds": 9999}})
        session.candidate_pools = [{"id": "p1", "candidates": ["A" * 180]},
                                   {"id": "p2", "candidates": ["B"]}]
        session.start()
        try:
            self.assertTrue(first.wait(1))
            session.pause()
            release.set()
            self.assertFalse(second.wait(.15))
            self.assertEqual(sent, [])
            session.resume()
            self.assertTrue(second.wait(1))
            deadline = time.monotonic() + 1
            while session.prepared_rounds != 1 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(session.prepared_rounds, 1)
            session._thread.join(1)
            self.assertEqual(len(sent), 1)
        finally:
            release.set()
            self.stop(session)
        self.assertFalse(session._thread.is_alive())

    def test_stop_during_preparation_discards_inflight_result_without_delivery(self):
        entered, release = threading.Event(), threading.Event()
        sent = []
        def synthesize(*_args):
            entered.set()
            release.wait(2)
            return b"RIFF"
        session = self.make_session(synthesize, lambda *args: sent.append(args))
        session.candidate_pools = [{"id": "p1", "candidates": ["A" * 180]},
                                   {"id": "p2", "candidates": ["B"]}]
        session.start()
        try:
            self.assertTrue(entered.wait(1))
            session.stop()
        finally:
            release.set()
            session._thread.join(1)
        self.assertEqual(session.status, "stopped")
        self.assertEqual(sent, [])
        self.assertEqual(session.prepared_segments, 0)

    def test_preparation_failure_never_delivers_partial_rounds(self):
        calls, sent = [], []
        def synthesize(text, _voice):
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError("tts down")
            return b"RIFF"
        session = self.make_session(synthesize, lambda *args: sent.append(args))
        session.candidate_pools = [{"id": "p1", "candidates": ["A" * 180]},
                                   {"id": "p2", "candidates": ["B"]}]
        session.start()
        session._thread.join(1)
        self.assertEqual(session.status, "failed")
        self.assertEqual(session.error, "tts down")
        self.assertEqual(sent, [])

    def test_paused_delivery_and_stop_do_not_send_another_item(self):
        paused, resumed = threading.Event(), threading.Event()
        sent = []
        def enqueue(*args):
            sent.append(args)
            session.pause()
            (paused if len(sent) == 1 else resumed).set()
        session = self.make_session(lambda *_: b"RIFF", enqueue)
        session.start()
        self.assertTrue(paused.wait(1))
        time.sleep(.1)
        self.assertEqual(len(sent), 1)
        session.resume()
        self.assertTrue(resumed.wait(1))
        self.stop(session)
        self.assertEqual(len(sent), 2)
        self.assertEqual(session.status, "stopped")

    def test_no_ack_and_disconnected_client_do_not_limit_sending(self):
        sent = []
        def enqueue(*args):
            sent.append(args)
            if len(sent) == 12:
                session.stop()
        session = self.make_session(lambda *_: b"RIFF", enqueue,
            cache_status=lambda _: {"ready": 100, "processing": 100, "client_connected": False})
        session.start()
        session._thread.join(1)
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(len(sent), 12)
        self.assertEqual([a[1] for a in sent], list(range(1,13)))

    def test_dynamic_time_is_rejected_before_any_synthesis(self):
        calls = []
        session = self.make_session(lambda *_: calls.append(True) or b"RIFF", lambda *_: None)
        session.candidate_pools = [{"id": "p1", "candidates": ["现在是{{current_time}}。"]}]
        session.start()
        session._thread.join(1)
        self.assertEqual(session.status, "failed")
        self.assertIn("时间占位符", session.error)
        self.assertEqual(calls, [])

    def test_preparation_retry_is_not_blocked_by_windows(self):
        calls = []
        def synthesize(text, _voice):
            calls.append(text)
            if len(calls) == 1:
                raise RetryableTTSClientError("busy", status_code=503)
            return b"RIFF"
        session = self.make_session(synthesize, lambda *_: session.stop(),
            cache_status=lambda _: {"client_connected": False})
        with patch.object(RetryableTTSClientError, "retry_delay", return_value=.01):
            session.start()
            session._thread.join(1)
        self.assertEqual(session.status, "stopped")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(session.prepared_rounds, 1)

    def test_public_factory_uses_prepared_loop_manager(self):
        self.assertIsInstance(build_live_manager("http://unused", "http://unused", "test"),
                              SynthesisBlockLiveSessionManager)

    def test_preparation_uses_independent_candidate_choices_without_fixed_seed(self):
        session = self.make_session(lambda *_: b"RIFF", lambda *_: None)
        session.candidate_pools = [{"id": "p1", "candidates": ["A1", "A2", "A3"]},
                                   {"id": "p2", "candidates": ["B1", "B2", "B3"]}]
        with patch.object(session._random, "randrange", side_effect=[2, 0]), patch.object(session._random, "seed") as seed:
            prepared = session._prepare()
        seed.assert_not_called()
        self.assertEqual(prepared[0].blocks[0].text, "A3\nB1")

    def test_http_delivery_reuses_audio_with_new_ids_and_playable_continuous_sequences(self):
        root = Path(tempfile.mkdtemp(prefix="tts-prepared-http-"))
        cache = AudioCacheManager(root / "server")
        class QuietHandler(make_handler(cache)):
            def log_message(self, *_args):
                pass
            def _timeline(self, *_args):
                pass
        server = AudioCacheServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        calls = []
        def synthesize(text, _voice):
            calls.append(text)
            if len(calls) == 9:
                manager._session.stop()
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(text.encode()[:1] + b"\x00" * (13 * 8000 * 2 - 1))
            return output.getvalue()
        url = f"http://127.0.0.1:{server.server_port}"
        manager = SynthesisBlockLiveSessionManager("http://unused", url, tts_api_key="test", synthesize=synthesize)
        try:
            with patch("server.prepared_live_session.PREPARED_AUDIO_ROOT", root / "prepared"):
                result = manager.start("default", [{"id": "p1", "candidates": ["A", "B"]}])
                self.assertEqual(result["preparation_total_rounds"], 1)
                client = AudioClient(url, root / "client")
                received = []
                deadline = time.monotonic() + 5
                while len(received) < 8 and time.monotonic() < deadline:
                    item = client.fetch_next()
                    if item is None:
                        time.sleep(.01)
                        continue
                    self.assertEqual(manager._session.prepared_rounds, 1)
                    client.ack(item.id)
                    received.append(item)
                self.assertEqual(len(received), 8)
                self.assertGreaterEqual(len(calls), 8)
                self.assertEqual([item.metadata["sequence"] for item in received], list(range(1, 9)))
                self.assertEqual(len({item.id for item in received}), 8)
                controller = PlaybackController(root / "client")
                for expected in received:
                    selected = controller._next_item()
                    self.assertIsNotNone(selected)
                    self.assertEqual(selected.item_id, expected.id)
                    controller._set_status(selected, "played")
        finally:
            if manager._session:
                self.stop(manager._session)
            server.shutdown()
            server.server_close()
            thread.join(1)


if __name__ == "__main__":
    unittest.main()
