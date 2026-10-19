"""Independent generation/playback controller behavior, without Windows or GPU."""
import time
import unittest

from server.live_session import LiveSessionManager, LiveSessionError


class GenerationPlaybackSplitTests(unittest.TestCase):
    def make_manager(self):
        events = []
        controls = {}

        def request(method, path, body=None, cache=False, timeout=None):
            events.append((method, path, body))
            if path == "/audio/session-control" and method == "POST":
                controls[body["session_id"]] = body["status"]
                return {"status": body["status"]}
            if path.startswith("/audio/session-control/") and method == "GET":
                session = path.rsplit("/", 1)[-1]
                return {"status": controls.get(session, "running")}
            if path.startswith("/audio/session-status/"):
                return {"ready": 2, "client_connected": True}
            return {}

        manager = LiveSessionManager(
            "http://gateway", "http://cache", synthesize=lambda *_: b"RIFF"
        )
        manager._request_json = request
        return manager, events, controls

    def test_generation_starts_without_playback_and_playback_runs_independently(self):
        manager, events, controls = self.make_manager()
        result = manager.start(
            "default", [{"id": "p1", "text": "第一段"}], autoplay=False
        )
        session = result["session_id"]
        self.assertEqual(controls[session], "paused")
        self.assertEqual(manager.playback_status()["status"], "paused")
        self.assertEqual(manager.playback_control("start")["status"], "running")
        self.assertEqual(manager.playback_control("pause")["status"], "paused")
        self.assertEqual(manager.playback_control("stop")["status"], "stopped")
        # Playback stop does not stop the generation session or request a GPU stop.
        self.assertTrue(manager.status()["session_id"] == session)
        deadline = time.monotonic() + 2
        while manager.status()["status"] in ("starting", "running") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(controls[session], "stopped")
        self.assertEqual(manager.playback_control("start")["status"], "running")
        with self.assertRaises(LiveSessionError):
            manager.reset()
        manager.playback_control("stop")
        manager.reset()
        self.assertEqual(manager.status()["status"], "idle")

    def test_legacy_start_retains_automatic_playback(self):
        manager, events, controls = self.make_manager()
        result = manager.start("default", [{"id": "p1", "text": "测试"}])
        session = result["session_id"]
        self.assertIn(controls[session], ("running", "stopped"))
        with self.assertRaises(LiveSessionError):
            manager.playback_control("pause")
        deadline = time.monotonic() + 2
        while manager.status()["status"] in ("starting", "running") and time.monotonic() < deadline:
            time.sleep(0.01)
        manager.reset()


if __name__ == "__main__":
    unittest.main()
