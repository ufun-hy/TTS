"""PCM RMS chunking regression on Python 3.11 and 3.13."""
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from recording_transcript.windows_chunks import _pcm_rms, split_wav


class WindowsASRChunksTests(unittest.TestCase):
    def test_integer_rms_for_both_pcm_widths(self):
        for width, samples in ((2, (-1000, 0, 1000, 0)), (4, (-65536000, 0, 65536000, 0))):
            raw = struct.pack("<" + ("h" if width == 2 else "i") * len(samples), *samples)
            expected = math.isqrt(sum(x * x for x in samples) // len(samples))
            self.assertEqual(_pcm_rms(raw, width), expected)
        self.assertEqual(_pcm_rms(b"", 2), 0)

    def test_chunking_does_not_drop_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.wav"
            rate = 1000
            raw = struct.pack("<" + "h" * (rate * 4), *([1000] * rate * 4))
            with wave.open(str(source), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(rate)
                writer.writeframes(raw)
            chunks = split_wav(source, root / "output", target_seconds=1.0, max_seconds=1.5)
            self.assertEqual(chunks[0].start, 0)
            self.assertAlmostEqual(chunks[-1].end, 4.0)
            frames = 0
            for item in chunks:
                with wave.open(str(item.path), "rb") as reader:
                    frames += reader.getnframes()
            self.assertEqual(frames, rate * 4)


if __name__ == "__main__":
    unittest.main()
