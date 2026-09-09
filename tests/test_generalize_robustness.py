import io
import json
import unittest
from unittest.mock import patch

from timeline.engine import Ollama, Segment, SegmentFailure, TimelineEngine, _review


class DirectLLM:
    def __init__(self):
        self.prompts = []

    def json(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return {
            "candidates": [
                "这款桃子今天还是23块8，两斤装，汁水足，喜欢的朋友可以直接看看。",
                "朋友们，两斤装的桃子今天23块8，水分很足，需要的可以了解一下。",
            ]
        }


class GeneralizeTests(unittest.TestCase):
    def test_model_writes_text_directly(self):
        llm = DirectLLM()
        segment = Segment("seg_001", 0, 10, "这款桃子两斤装23块8，汁水很足。")
        result = TimelineEngine(llm, variant_count=2).process_one(segment, max_retries=0)
        self.assertEqual(len(result["segment"]["candidates"]), 2)
        self.assertIn(segment.original_text, llm.prompts[0])
        self.assertNotIn("semantic_points", llm.prompts[0])
        self.assertNotIn("hard_keep", llm.prompts[0])

    def test_program_does_not_score_or_reject_text_quality(self):
        segment = Segment("seg_001", 0, 10, "一段很长的原话")
        review = _review("很短", segment)
        self.assertTrue(review["accepted"])
        self.assertEqual(review["reasons"], [])

    def test_output_drops_analysis_and_review_fields(self):
        result = TimelineEngine(DirectLLM()).process_one(Segment("seg", 0, 5, "原话"), max_retries=0)
        output = result["segment"]
        for key in (
            "segment_type", "intent", "facts", "must_keep", "hard_keep",
            "semantic_keep", "semantic_points", "duration_status",
            "duration_ratio", "surface_similarity", "semantic_coverage",
        ):
            self.assertNotIn(key, output)
        self.assertIn("candidates", output)

    def test_only_invalid_structure_retries(self):
        class RetryLLM:
            def __init__(self):
                self.calls = 0
            def json(self, prompt, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"bad": []}
                return {"candidates": ["第二次返回正常文本。"]}

        llm = RetryLLM()
        result = TimelineEngine(llm).process_one(Segment("seg", 0, 5, "原话"), max_retries=1)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["segment"]["candidates"], ["第二次返回正常文本。"])

    def test_empty_candidates_fail_transport_validation(self):
        class EmptyLLM:
            def json(self, prompt, **_kwargs):
                return {"candidates": []}

        with self.assertRaises(SegmentFailure):
            TimelineEngine(EmptyLLM()).process_one(Segment("seg", 0, 5, "原话"), max_retries=0)

    def test_batch_failure_does_not_stop_following_segments(self):
        class BatchLLM:
            def json(self, prompt, **_kwargs):
                if "坏段" in prompt:
                    return {"bad": []}
                return {"candidates": ["正常文本"]}

        segments = [
            Segment("a", 0, 3, "正常段"),
            Segment("b", 3, 6, "坏段"),
            Segment("c", 6, 9, "正常段"),
        ]
        result = TimelineEngine(BatchLLM()).process_batch(segments, max_retries=0)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failed"], 1)

    def test_ollama_retries_malformed_json(self):
        invalid = io.BytesIO(json.dumps({"response": "不是 JSON"}).encode())
        valid = io.BytesIO(json.dumps({"response": '{"candidates":["可以"]}'}).encode())
        with patch("timeline.engine.request.urlopen", side_effect=[invalid, valid]):
            result = Ollama("test", json_retry_count=1).json("prompt", '{"candidates":["..."]}')
        self.assertEqual(result["candidates"], ["可以"])


if __name__ == "__main__":
    unittest.main()
