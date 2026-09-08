import unittest

from timeline.engine import Analysis, Segment, TimelineEngine, estimate_duration


class FakeLLM:
    def json(self, prompt):
        if "分析器" in prompt:
            return {"segment_type": "benefit", "intent": "说明产品使用方便", "facts": ["操作更方便"], "must_keep": ["操作更方便"], "tone": "口语", "mode": "atomic"}
        return {"variants": ["这个设计用起来会更顺手，操作更方便。", "实际使用的时候，操作更方便，整个过程也省事。"]}


class TimelineTests(unittest.TestCase):
    def test_duration_is_stable_and_nonzero(self):
        self.assertAlmostEqual(estimate_duration("大家好。"), 1.009, places=2)

    def test_process_keeps_order_and_filters_invalid_output(self):
        segments = [
            Segment("seg_1", 0, 5, "原话一", Analysis(mode="atomic", intent="一", must_keep=["操作更方便"])),
            Segment("seg_2", 5, 10, "原话二", Analysis(mode="atomic", intent="二", must_keep=["操作更方便"])),
        ]
        result = TimelineEngine(FakeLLM(), seed=1, simple_candidate=False).process(segments)
        self.assertEqual([item["id"] for item in result["segments"]], ["seg_1", "seg_2"])
        self.assertTrue(result["segments"][0]["variants"])

    def test_choose_avoids_recent_variant(self):
        segment = Segment("seg_1", 0, 5, "原话", Analysis(mode="atomic", intent="一"), variants=["A", "B"])
        self.assertEqual(TimelineEngine(FakeLLM(), seed=1).choose(segment, ["A"]), "B")


if __name__ == "__main__":
    unittest.main()
