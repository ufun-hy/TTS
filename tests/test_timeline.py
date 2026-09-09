import unittest
from timeline.engine import Segment, TimelineEngine, estimate_duration


class FakeLLM:
    def json(self, prompt, **_kwargs):
        return {"candidates": ["这个设计用起来会更顺手。", "实际使用的时候整个过程也更省事。"]}


class TimelineTests(unittest.TestCase):
    def test_duration_estimate_remains_available_for_runtime_reporting(self):
        self.assertAlmostEqual(estimate_duration("大家好。"), 1.009, places=2)

    def test_process_keeps_order(self):
        segments = [Segment("seg_1", 0, 5, "原话一"), Segment("seg_2", 5, 10, "原话二")]
        result = TimelineEngine(FakeLLM(), seed=1).process(segments)
        self.assertEqual([item["id"] for item in result["segments"]], ["seg_1", "seg_2"])
        self.assertTrue(result["segments"][0]["candidates"])

    def test_choose_avoids_recent_candidate(self):
        segment = Segment("seg_1", 0, 5, "原话", candidates=["A", "B"])
        self.assertEqual(TimelineEngine(FakeLLM(), seed=1).choose(segment, ["A"]), "B")


if __name__ == "__main__":
    unittest.main()
