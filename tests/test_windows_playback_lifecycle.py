import threading
import unittest

from audio_client.gui import AudioClientApp


class _Value:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class _Button:
    def configure(self, **kwargs):
        self.state = kwargs.get("state", "")


class _Playback:
    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def stop(self):
        self.calls.append("stop")


class _Logger:
    def info(self, *_args):
        pass


class WindowsPlaybackLifecycleTests(unittest.TestCase):
    @staticmethod
    def app():
        app = AudioClientApp.__new__(AudioClientApp)
        app.playback = _Playback()
        app.stop_event = threading.Event()
        app._download_paused = threading.Event()
        app._local_paused = threading.Event()
        app._remote_paused = threading.Event()
        app._playback_requested = True
        app.status_var = _Value()
        app.start_button = _Button()
        app.stop_button = _Button()
        app.logger = _Logger()
        return app

    def test_pause_resume_gates_download_and_playback(self):
        app = self.app()
        app.pause_playback()
        self.assertTrue(app._download_paused.is_set())
        self.assertTrue(app._local_paused.is_set())
        self.assertEqual(app.playback.calls, ["pause"])
        app.resume_playback()
        self.assertFalse(app._download_paused.is_set())
        self.assertFalse(app._local_paused.is_set())
        self.assertEqual(app.playback.calls, ["pause", "resume"])

    def test_stop_stops_network_and_playback_for_next_session(self):
        app = self.app()
        app.stop_playback()
        self.assertTrue(app.stop_event.is_set())
        self.assertTrue(app._download_paused.is_set())
        self.assertFalse(app._playback_requested)
        self.assertEqual(app.playback.calls, ["stop"])
        self.assertEqual(app.status_var.value, "已停止")


if __name__ == "__main__":
    unittest.main()
