import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave

from audio_client.playback import PlaybackStopped, WinMMPlayer


@unittest.skipUnless(os.name == "nt", "WinMM smoke test runs on Windows only")
class WinMMSmokeTests(unittest.TestCase):
    def test_open_play_status_stop_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smoke.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"".join(
                    int(10000 * math.sin(2 * math.pi * 440 * index / 8000)).to_bytes(2, "little", signed=True)
                    for index in range(8000)
                ))

            stopped = threading.Event()
            errors = []

            def play():
                try:
                    WinMMPlayer().play(path, stopped, threading.Event())
                except PlaybackStopped:
                    pass
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=play)
            thread.start()
            time.sleep(0.25)
            stopped.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "WinMM playback did not stop")
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
