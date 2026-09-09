import unittest
from timeline.engine import Segment, SegmentFailure, TimelineEngine


class DirectLLM:
    def __init__(self):
        self.prompts = []

    def json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return {"candidates": ["版本A", "版本B"]}


class GeneralizeTests(unittest.TestCase):
    def test_direct_model_only(self):
        llm = DirectLLM()
        result = TimelineEngine(llm, variant_count=2).process_one(Segment("s", 0, 5, "原话"), max_retries=0)
        self.assertEqual(result["segment"]["candidates"], ["版本A", "版本B"])
        prompt = llm.prompts[0]
        for word in ("hard_keep", "semantic_points", "surface_similarity", "duration_status"):
            self.assertNotIn(word, prompt)

    def test_output_is_minimal(self):
        output = TimelineEngine(DirectLLM()).process_one(Segment("s", 0, 5, "原话"), 0)["segment"]
        for key in ("analysis", "facts", "intent", "hard_keep", "semantic_points", "review"):
            self.assertNotIn(key, output)

    def test_schema_retry(self):
        class RetryLLM:
            def __init__(self):
                self.calls = 0

            def json(self, prompt, **kwargs):
                self.calls += 1
                return {"bad": []} if self.calls == 1 else {"candidates": ["ok"]}

        result = TimelineEngine(RetryLLM()).process_one(Segment("s", 0, 5, "原话"), 1)
        self.assertEqual(result["segment"]["candidates"], ["ok"])

    def test_empty_fails(self):
        class EmptyLLM:
            def json(self, prompt, **kwargs):
                return {"candidates": []}

        with self.assertRaises(SegmentFailure):
            TimelineEngine(EmptyLLM()).process_one(Segment("s", 0, 5, "原话"), 0)


if __name__ == "__main__":
    unittest.main()
