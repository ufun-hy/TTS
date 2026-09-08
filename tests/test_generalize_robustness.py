import io
import json
import unittest
from unittest.mock import patch

from timeline.engine import Analysis, Ollama, Segment, TimelineEngine


class SequenceLLM:
    def __init__(self):
        self.calls = 0

    def json(self, prompt, **_kwargs):
        self.calls += 1
        if "bad" in prompt:
            return {"variants": []}
        if self.calls == 1:
            return {"variants": ["短"]}
        return {"variants": ["这是一段足够完整的直播口播表达。"]}


class RobustnessTests(unittest.TestCase):
    def test_ollama_retries_malformed_json(self):
        invalid = io.BytesIO(json.dumps({"response": "不是 JSON"}).encode())
        valid = io.BytesIO(json.dumps({"response": '{"variants": ["可以"]}'}).encode())
        with patch("timeline.engine.request.urlopen", side_effect=[invalid, valid]):
            result = Ollama("test", json_retry_count=1).json("prompt", "{variants:[]}")
        self.assertEqual(result["variants"], ["可以"])

    def test_duration_retry_keeps_analyzer_result(self):
        llm = SequenceLLM()
        segment = Segment("seg_001", 0, 4, "原话", Analysis(intent="保留意思"))
        result = TimelineEngine(llm).process_one(segment, max_retries=1)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(llm.calls, 2)

    def test_failed_segment_does_not_stop_batch(self):
        class BatchLLM:
            def json(self, prompt, **_kwargs):
                if "bad" in prompt:
                    return {"variants": []}
                return {"variants": ["这是一段正常直播口播表达。"]}

        segments = [
            Segment("seg_001", 0, 3, "good", Analysis(intent="一")),
            Segment("seg_002", 3, 6, "bad", Analysis(intent="二")),
            Segment("seg_003", 6, 9, "good", Analysis(intent="三")),
        ]
        result = TimelineEngine(BatchLLM()).process_batch(segments, max_retries=1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual([item["id"] for item in result["segments"]], ["seg_001", "seg_002", "seg_003"])
        self.assertEqual(result["failed_segments"][0]["segment_id"], "seg_002")


if __name__ == "__main__":
    unittest.main()
