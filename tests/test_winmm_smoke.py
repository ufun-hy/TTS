import math
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import Mock

from audio_client.playback import PlaybackStopped, WinMMPlayer


class FakeMCI:
    def __init__(self):
        self.commands = []

    def __call__(self, command, target, _length, _callback):
        self.commands.append(command)
        if command.startswith("status "):
            target.value = "stopped"
        return 0


class WinMMPreparationTests(unittest.TestCase):
    def test_mci_open_uses_derived_pcm16_path_for_float_source(self):
        from audio_client.wav_compat import prepare_mci_wav

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "float.wav"
            frames = b"".join(struct.pack("<f", value) for value in (0.0, 0.5, -0.5))
            fmt = struct.pack("<HHIIHH", 3, 1, 24000, 24000 * 4, 4, 32)
            body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
            source.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)

            fake = FakeMCI()
            player = WinMMPlayer()
            player._mci = fake
            player._error = None
            player.play(source, threading.Event(), threading.Event())
            prepared = prepare_mci_wav(source)
            open_command = next(command for command in fake.commands if command.startswith("open "))
            self.assertIn(str(prepared), open_command)
            self.assertIn(f"{prepared.parent.name}", open_command)
            self.assertNotIn(f'open "{source}" type', open_command)

    def test_compatibility_log_reports_format_cache_and_duration_without_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "float.wav"
            frames = b"".join(struct.pack("<f", value) for value in (0.0, 0.5, -0.5))
            fmt = struct.pack("<HHIIHH", 3, 1, 24000, 24000 * 4, 4, 32)
            body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
            source.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
            logger = Mock()
            fake = FakeMCI()
            player = WinMMPlayer(logger=logger)
            player._mci = fake
            player._error = None
            player.play(source, threading.Event(), threading.Event())
            messages = [call.args[0] for call in logger.info.call_args_list]
            self.assertTrue(any("input_format=%s" in message and "cache=%s" in message and "conversion_ms=%d" in message for message in messages))
            rendered = " ".join(str(value) for call in logger.info.call_args_list for value in call.args)
            self.assertIn("float32", rendered)
            self.assertIn("pcm16", rendered)
            self.assertNotIn("0.5", rendered)


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

    def test_open_play_float32_source_after_compat_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "float.wav"
            frames = b"".join(struct.pack("<f", 0.15 * math.sin(2 * math.pi * 440 * index / 24000)) for index in range(24000))
            fmt = struct.pack("<HHIIHH", 3, 1, 24000, 24000 * 4, 4, 32)
            body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
            path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)

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
            self.assertFalse(thread.is_alive(), "WinMM Float32 playback did not stop")
            self.assertEqual(errors, [])
            self.assertEqual(len(list((path.parent / "pcm16").glob("*.wav"))), 1)


if __name__ == "__main__":
    unittest.main()
