import importlib.util
from pathlib import Path
import unittest

from timeline.resegmenter import resegment


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audio-ingest.py"
SPEC = importlib.util.spec_from_file_location("audio_ingest", SCRIPT)
audio_ingest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audio_ingest)


class AudioIngestTests(unittest.TestCase):
    def test_normalize_result_keeps_segment_and_word_timestamps(self):
        result = audio_ingest.normalize_result(
            {
                "language": "zh",
                "segments": [{
                    "start": 0,
                    "end": 4.2,
                    "text": "今天我们先看产品",
                    "words": [
                        {"word": "今天", "start": 0, "end": 0.42},
                        {"word": "我们", "start": 0.5, "end": 0.8},
                        {"word": "先看", "start": 0.9, "end": 1.2},
                        {"word": "产品", "start": 1.3, "end": 1.6},
                    ],
                }],
            },
            Path("live.mp3"),
            4.2,
            "zh",
        )
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["segments"][0]["id"], "asr_0001")
        self.assertEqual(result["segments"][0]["words"][1]["end"], 0.8)
        self.assertEqual(result["source"], str(Path("live.mp3").resolve()))
        children, report = resegment(result["segments"])
        self.assertEqual("".join(item["text"] for item in children), result["segments"][0]["text"])
        self.assertEqual(report["text_retention"], 1.0)

    def test_missing_word_timestamps_is_rejected(self):
        with self.assertRaises(audio_ingest.IngestError):
            audio_ingest.normalize_result(
                {"segments": [{"start": 0, "end": 1, "text": "没有词级时间戳"}]},
                Path("live.wav"),
                1,
                "zh",
            )

    def test_small_decoder_boundary_drift_is_clamped(self):
        result = audio_ingest.normalize_result(
            {"segments": [{"start": 0, "end": 1, "text": "测试", "words": [{"word": "测试", "start": 0, "end": 1.02}]}]},
            Path("live.wav"),
            1,
            "zh",
        )
        self.assertEqual(result["segments"][0]["words"][0]["end"], 1.0)

    def test_backend_order_prefers_mlx(self):
        original = audio_ingest.importlib.util.find_spec
        try:
            audio_ingest.importlib.util.find_spec = lambda name: object() if name in {"mlx_whisper", "whisper"} else None
            self.assertEqual(audio_ingest.choose_backend("auto"), "mlx-whisper")
        finally:
            audio_ingest.importlib.util.find_spec = original

    def test_whisper_cpp_time_supports_json_timestamp_and_offsets(self):
        self.assertAlmostEqual(audio_ingest._parse_whisper_cpp_time("00:01:02,500"), 62.5)
        self.assertAlmostEqual(audio_ingest._parse_whisper_cpp_time(1250), 1.25)


if __name__ == "__main__":
    unittest.main()
