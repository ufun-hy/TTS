import unittest
from copy import deepcopy

from voice_datasets.transcription import candidate_segments, union_duration, windows
from voice_datasets.review import statistics, validate_review


class DatasetTests(unittest.TestCase):
    def test_windows_cover_all_source_without_duration_cap(self):
        chunks = list(windows(15002.279167, 600, 5))
        self.assertEqual(len(chunks), 26)
        self.assertEqual(chunks[-1]["core_end"], 15002.279167)
        self.assertAlmostEqual(union_duration([(c["core_start"], c["core_end"]) for c in chunks]), 15002.279167)
        self.assertEqual(chunks[1]["decode_start"], 595)
        self.assertEqual(union_duration([(0, 10), (5, 15), (20, 24)]), 19)

    def test_asr_remains_pending_and_uses_original_time(self):
        chunk = {"window": list(windows(1300, 600, 5))[1], "result": {"segments": [
            {"start": 4, "end": 10, "text": "商品二十九块九", "words": [{"start": 4, "end": 4, "word": "商品"}]}]}}
        rows = candidate_segments(chunk, {"source_file": "original.mp3", "speaker_id": "speaker_b"}, 1300)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_time"], 599)
        self.assertEqual(rows[0]["end_time"], 605)
        self.assertFalse(rows[0]["text_verified"])
        self.assertFalse(rows[0]["natural_boundary_verified"])
        self.assertEqual(rows[0]["quality_status"], "pending")
        self.assertIn("word_timing_review", rows[0]["review_flags"])
        self.assertIn("window_seam_review", rows[0]["review_flags"])

    def test_overlap_midpoint_ownership(self):
        result = {"segments": [{"start": 600, "end": 604, "text": "交界处"}]}
        rows = candidate_segments({"window": list(windows(1300, 600, 5))[0], "result": result},
                                  {"source_file": "original.mp3", "speaker_id": "speaker_a"}, 1300)
        self.assertEqual(rows, [])

    def test_invalid_times_are_rejected(self):
        for interval in [[(0, float("nan"))], [(2, 1)], [(-1, 5)]]:
            with self.assertRaises(ValueError):
                union_duration(interval)
        with self.assertRaises(ValueError):
            list(windows(10, 0, 0))

    def test_review_gate_and_no_duration_quota(self):
        sources = [{"source_file": "original.mp3", "speaker_id": "speaker_a", "duration": 20000,
                    "authorization": "authorized by user"}]
        row = {"source_file": "original.mp3", "speaker_id": "speaker_a", "utterance_id": "a_000001",
               "start_time": 0, "end_time": 32, "text": "校对后的完整表达", "text_verified": True,
               "speaker_verified": True, "natural_boundary_verified": True,
               "quality_status": "accepted", "reviewer": "reviewer"}
        other = {**row, "utterance_id": "a_000002", "start_time": 32, "end_time": 36}
        result = statistics([row, other], sources)
        self.assertEqual(result["usable_audio_duration"], 36)
        self.assertEqual(result["min_duration"], 4)
        self.assertEqual(result["max_duration"], 32)
        self.assertEqual(result["raw_audio_duration"], 20000)
        self.assertEqual(result["pending_duration"], 19964)
        for key, value in [("text_verified", False), ("speaker_verified", False),
                           ("natural_boundary_verified", False), ("reviewer", None),
                           ("speaker_id", "speaker_b"), ("utterance_id", "../unsafe"),
                           ("end_time", float("nan"))]:
            changed = {**row, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_review([changed], sources)
        self.assertEqual(validate_review([{**row, "quality_status": "pending", "text_verified": False}], sources), [])
        with self.assertRaises(ValueError):
            validate_review([row, {**other, "start_time": 31}], sources)
        with self.assertRaises(ValueError):
            validate_review([row, deepcopy(row)], sources)


if __name__ == "__main__":
    unittest.main()
