import unittest
from timeline.engine import GENERALIZE_STRATEGY_VERSION, Segment, SegmentFailure, TimelineEngine


class DirectLLM:
    def __init__(self):
        self.prompts = []
        self.kwargs = []

    def json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        return {"candidates": ["自然改写A", "自然改写B"]}


class GeneralizeTests(unittest.TestCase):
    def test_direct_model_v2_prompt_only(self):
        llm = DirectLLM()
        result = TimelineEngine(llm, variant_count=2).process_one(Segment("s", 0, 5, "原话"), max_retries=0)
        self.assertEqual(result["segment"]["candidates"], ["自然改写A", "自然改写B"])
        self.assertEqual(GENERALIZE_STRATEGY_VERSION, "direct-model-v2")
        prompt = llm.prompts[0]
        self.assertIn("不要总结、概括或删减", prompt)
        self.assertIn("物流、售后", prompt)
        for word in (
            "hard_keep",
            "semantic_points",
            "surface_similarity",
            "duration_status",
            "版本1",
            "版本2",
            "版本3",
        ):
            self.assertNotIn(word, prompt)
        schema_hint = llm.kwargs[0].get("schema_hint", "")
        self.assertNotIn("版本", schema_hint)
        self.assertNotIn("...", schema_hint)

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
