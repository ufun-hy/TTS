import io
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave
from unittest.mock import patch

from audio_client.wav_compat import (
    CONVERSION_VERSION,
    WavCompatibilityError,
    is_float32_wav,
    prepare_mci_wav,
)


PCM_GUID = bytes.fromhex("01000000 0000 1000 8000 00aa00389b71")
FLOAT_GUID = bytes.fromhex("03000000 0000 1000 8000 00aa00389b71")


def pcm16_wav(samples=(0, 1000, -1000), rate=24000, channels=1, extra=b""):
    frames = b"".join(struct.pack("<h", value) for value in samples)
    return _wav(1, channels, rate, 16, frames, extra=extra)


def float32_wav(samples=(0.0, 0.5, -0.5, 1.0, -1.0), rate=24000, channels=1, extensible=False, extra=b""):
    frames = b"".join(struct.pack("<f", value) for value in samples)
    return _wav(3, channels, rate, 32, frames, extensible=extensible, extra=extra)


def _wav(code, channels, rate, bits, frames, extensible=False, extra=b""):
    align = channels * (bits // 8)
    if extensible:
        fmt = struct.pack("<HHIIHHH", 0xFFFE, channels, rate, rate * align, align, bits, 22)
        fmt += struct.pack("<HI", bits, 0) + (FLOAT_GUID if code == 3 else PCM_GUID)
    else:
        fmt = struct.pack("<HHIIHH", code, channels, rate, rate * align, align, bits)
    def chunk(name, payload):
        padding = b"\0" if len(payload) & 1 else b""
        return name + struct.pack("<I", len(payload)) + payload + padding
    body = b"WAVE" + chunk(b"JUNK", extra) + chunk(b"fmt ", fmt) + chunk(b"data", frames)
    return b"RIFF" + struct.pack("<I", len(body)) + body


class WavCompatTests(unittest.TestCase):
    def test_pcm16_is_validated_and_returned_without_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pcm.wav"
            source.write_bytes(pcm16_wav())
            self.assertEqual(prepare_mci_wav(source), source)
            self.assertFalse((source.parent / "pcm16").exists())

    def test_float32_converts_to_pcm16_and_preserves_shape_and_clips(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "float.wav"
            source.write_bytes(float32_wav((0.0, 0.5, -0.5, 2.0, -2.0)))
            output = prepare_mci_wav(source)
            self.assertEqual(output.parent.name, "pcm16")
            with wave.open(str(output), "rb") as reader:
                self.assertEqual((reader.getnchannels(), reader.getframerate(), reader.getsampwidth()), (1, 24000, 2))
                self.assertEqual(reader.getnframes(), 5)
                self.assertEqual(struct.unpack("<5h", reader.readframes(5)), (0, 16384, -16384, 32767, -32768))
            self.assertTrue(source.read_bytes().startswith(b"RIFF"))

    def test_extensible_float32_and_pcm16_subformats_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            float_source = root / "ext-float.wav"
            pcm_source = root / "ext-pcm.wav"
            float_source.write_bytes(float32_wav(extensible=True))
            pcm_source.write_bytes(_wav(1, 2, 16000, 16, struct.pack("<4h", 1, -1, 2, -2), extensible=True))
            self.assertEqual(prepare_mci_wav(pcm_source), pcm_source)
            self.assertNotEqual(prepare_mci_wav(float_source), float_source)

    def test_conversion_cache_reuses_valid_output_and_rebuilds_after_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "same.wav"
            source.write_bytes(float32_wav())
            first = prepare_mci_wav(source)
            with patch("audio_client.wav_compat._convert_float32", side_effect=AssertionError("converted twice")):
                self.assertEqual(prepare_mci_wav(source), first)
            source.write_bytes(float32_wav((0.1, 0.2)))
            second = prepare_mci_wav(source)
            self.assertNotEqual(first, second)
            self.assertIn(f"v{CONVERSION_VERSION}", second.name)

    def test_extra_chunks_and_odd_padding_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chunks.wav"
            source.write_bytes(float32_wav((0.25,), extra=b"x"))
            output = prepare_mci_wav(source)
            self.assertTrue(output.is_file())

    def test_invalid_samples_and_structure_never_leave_visible_output(self):
        cases = [
            float32_wav((math.nan,)),
            float32_wav((math.inf,)),
            b"RIFF\x04\0\0\0WAVE",
            float32_wav()[:-1],
            _wav(3, 1, 24000, 32, b"\0" * 3),
            _wav(6, 1, 24000, 32, b"\0" * 4),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, content in enumerate(cases):
                source = Path(directory) / f"bad-{index}.wav"
                source.write_bytes(content)
                with self.assertRaises(WavCompatibilityError):
                    prepare_mci_wav(source)
                cache = source.parent / "pcm16"
                if cache.exists():
                    self.assertEqual(list(cache.glob(f".{source.name}*.tmp")), [])

    def test_invalid_extensible_guid_and_alignment_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_guid = root / "guid.wav"
            raw = bytearray(float32_wav(extensible=True))
            guid_offset = raw.index(FLOAT_GUID)
            raw[guid_offset:guid_offset + 16] = b"x" * 16
            bad_guid.write_bytes(raw)
            alignment = root / "alignment.wav"
            raw = bytearray(float32_wav())
            fmt = raw.index(b"fmt ") + 8
            struct.pack_into("<H", raw, fmt + 12, 2)
            alignment.write_bytes(raw)
            for source in (bad_guid, alignment):
                with self.assertRaises(WavCompatibilityError):
                    prepare_mci_wav(source)

    def test_source_change_during_conversion_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "changing.wav"
            source.write_bytes(float32_wav())
            original = Path.stat
            calls = {"count": 0}
            def changing_stat(path):
                value = original(path)
                if path == source:
                    calls["count"] += 1
                    if calls["count"] == 3:
                        source.write_bytes(float32_wav((0.1,)))
                return value
            with patch("audio_client.wav_compat.Path.stat", changing_stat):
                with self.assertRaises(WavCompatibilityError):
                    prepare_mci_wav(source)

    def test_float_detector_does_not_classify_bad_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.wav"
            source.write_bytes(b"not wav")
            self.assertFalse(is_float32_wav(source))


if __name__ == "__main__":
    unittest.main()
