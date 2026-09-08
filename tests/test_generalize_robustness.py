import io
import json
import unittest
from unittest.mock import patch

from timeline.duration_policy import DurationPolicy
from timeline.engine import Analysis, Ollama, Segment, SegmentFailure, TimelineEngine, _review
from timeline.semantic_coverage import coverage_report


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
    def test_natural_duration_policy_has_preferred_and_hard_ranges(self):
        policy = DurationPolicy()
        window = policy.window(10)
        self.assertEqual(window.status(7), "acceptable")
        self.assertEqual(window.status(13), "acceptable")
        self.assertEqual(window.status(2), "extreme_too_short")
        self.assertEqual(window.status(25), "extreme_too_long")

    def test_short_segment_allows_natural_absolute_deviation(self):
        policy = DurationPolicy()
        self.assertIn(policy.window(2.5).status(3.5), {"preferred", "acceptable"})

    def test_acceptable_duration_passes_with_warning(self):
        segment = Segment("seg_duration", 0, 10, "原话", Analysis(intent="说明", must_keep=[]))
        review = _review("这是一个自然完整的直播表达，整体节奏略有变化，但信息清楚、听起来顺口自然。", segment, segment.analysis, 0.15, duration_policy=DurationPolicy())
        self.assertTrue(review["accepted"])
        self.assertIn(review["duration_status"], {"preferred", "acceptable"})
        self.assertIn("duration_target", review)

    def test_short_composable_uses_compact_atomic(self):
        class CompactLLM:
            def json(self, prompt, **_kwargs):
                return {"variants": ["需要的现在就拍。", "喜欢的话直接下单。", "想要就赶紧拍。"]}

        segment = Segment("seg_cta", 0, 2.5, "想要的直接拍。", Analysis(segment_type="cta", intent="促单", mode="composable"))
        result = TimelineEngine(CompactLLM(), simple_candidate=False).process_one(segment, max_retries=0)
        self.assertEqual(result["segment"]["strategy"], "compact_atomic")
        self.assertTrue(result["segment"]["variants"])

    def test_precomposed_candidate_is_reviewed_before_slots(self):
        class CandidateLLM:
            def json(self, prompt, **_kwargs):
                return {"slots": [{"id": "opening", "required": True, "variants": ["太好了"]}], "candidates": ["朋友们这款桃子皮薄肉多汁水足，今天到手199元，喜欢的朋友可以直接下单，口感真的很不错。"]}

        analysis = Analysis(segment_type="price", intent="说明价格", mode="composable", must_keep=["199元"])
        segment = Segment("seg_candidate", 0, 10, "今天到手199元。", analysis)
        result = TimelineEngine(CandidateLLM(), simple_candidate=False).process_one(segment, max_retries=0)
        self.assertTrue(result["review"]["precomposed_candidate_pass"])
        self.assertTrue(result["segment"]["candidates"])

    def test_slot_failure_can_use_full_sentence_rescue(self):
        class RescueLLM:
            def json(self, prompt, **_kwargs):
                if "最终修复器" in prompt:
                    return {"candidates": ["朋友们今天这款桃子皮薄肉多汁水足，到手199元，喜欢的朋友可以直接下单，口感真的很不错。"]}
                return {"slots": [{"id": "opening", "required": True, "variants": ["太好了"]}], "candidates": []}

        analysis = Analysis(segment_type="price", intent="说明价格", mode="composable", must_keep=["199元"])
        segment = Segment("seg_rescue", 0, 10, "今天到手199元。", analysis)
        result = TimelineEngine(RescueLLM(), simple_candidate=False).process_one(segment, max_retries=0)
        self.assertTrue(result["review"]["rescue_rewrite_pass"])

    def test_missing_must_keep_still_fails(self):
        class UnsafeLLM:
            def json(self, prompt, **_kwargs):
                return {"slots": [{"id": "opening", "required": True, "variants": ["太好了"]}], "candidates": ["朋友们喜欢的可以直接下单。"]}

        analysis = Analysis(segment_type="price", intent="说明价格", mode="composable", must_keep=["199元"])
        segment = Segment("seg_fact", 0, 10, "今天到手199元。", analysis)
        with self.assertRaises(SegmentFailure) as error:
            TimelineEngine(UnsafeLLM(), simple_candidate=False).process_one(segment, max_retries=0)
        self.assertEqual(error.exception.reason, "composable_no_valid_combination")

    def test_batch_reports_global_duration_drift(self):
        class DriftLLM:
            def json(self, prompt, **_kwargs):
                return {"variants": ["这是一个自然完整的直播表达，时长会略有变化。"]}

        segments = [
            Segment("seg_a", 0, 10, "原话一", Analysis(intent="一")),
            Segment("seg_b", 10, 20, "原话二", Analysis(intent="二")),
        ]
        result = TimelineEngine(DriftLLM()).process_batch(segments, max_retries=0)
        self.assertIn("global_duration_drift", result["stats"])
        self.assertIn("original_total_speech_duration", result["stats"])

    def test_simple_rewrite_uses_skeleton_without_full_original(self):
        class CaptureLLM:
            def __init__(self):
                self.prompt = ""

            def json(self, prompt, **_kwargs):
                self.prompt = prompt
                return {"candidates": ["朋友们现在可以直接了解这款桃子，23块8，两斤装，汁水很足。"]}

        llm = CaptureLLM()
        original = "这是原始长句，不应该进入 Simple Candidate Rewrite。"
        analysis = Analysis(segment_type="promotion", intent="介绍价格和包装", facts=["汁水足"], must_keep=["23块8", "两斤装"], hard_keep=["23块8", "两斤装"], semantic_keep=["汁水足"], semantic_points=["强调汁水足", "说明价格", "说明两斤装"], mode="composable")
        segment = Segment("seg_skeleton", 0, 10, original, analysis)
        TimelineEngine(llm).process_one(segment, max_retries=0)
        self.assertNotIn(original, llm.prompt)
        self.assertIn("23块8", llm.prompt)
        self.assertIn("强调汁水足", llm.prompt)

    def test_hard_keep_retry_recovers_without_reanalyze(self):
        class RetryLLM:
            def __init__(self):
                self.calls = 0

            def json(self, prompt, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"candidates": ["朋友们这款桃子现在很划算，喜欢的可以看看。"]}
                return {"candidates": ["朋友们这款桃子现在23块8，两斤装，喜欢的可以直接了解。"]}

        llm = RetryLLM()
        analysis = Analysis(segment_type="promotion", intent="介绍价格和规格", hard_keep=["23块8", "两斤装"], semantic_points=["说明价格和规格"], mode="atomic")
        segment = Segment("seg_keep_retry", 0, 8, "原始促销话术", analysis)
        result = TimelineEngine(llm).process_one(segment, max_retries=2)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(llm.calls, 2)
        self.assertTrue(result["segment"]["candidates"])

    def test_semantic_coverage_tracks_multiple_points(self):
        report = coverage_report(["桃子皮薄", "汁水足", "成熟度合适", "不容易软塌", "老人小孩容易入口"], "这款桃子皮很薄，水分特别足，熟度刚刚好，不会软掉，老人小孩都好入口。")
        self.assertTrue(report["accepted"])
        self.assertTrue(report["complete"])
        self.assertEqual(report["covered"], 5)

    def test_semantic_coverage_missing_is_advisory_not_hard_failure(self):
        class CoverageLLM:
            def json(self, prompt, **_kwargs):
                return {"candidates": ["桃子皮薄，价格合适。", "桃子皮很薄，吃起来不错。"]}

        analysis = Analysis(segment_type="benefit", intent="介绍卖点", semantic_points=["桃子皮薄", "汁水足"], mode="atomic")
        segment = Segment("seg_coverage", 0, 8, "桃子皮薄汁水足", analysis)
        result = TimelineEngine(CoverageLLM()).process_one(segment, max_retries=0)
        self.assertTrue(result["segment"]["candidates"])
        self.assertTrue(result["review"]["sample"]["semantic_coverage"]["advisory"])
        self.assertIn("汁水足", result["review"]["sample"]["semantic_coverage"]["missing"])

    def test_semantic_coverage_hard_keep_does_not_cover_unrelated_point(self):
        report = coverage_report(["包装方便携带"], "今天23块8。", ["23块8"])
        self.assertTrue(report["accepted"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["missing"], ["包装方便携带"])

    def test_semantic_coverage_does_not_accept_opposite_polarity(self):
        report = coverage_report(["不容易软塌"], "这个桃子很容易软塌。")
        self.assertTrue(report["accepted"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["missing"], ["不容易软塌"])

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
        result = TimelineEngine(BatchLLM(), simple_candidate=False).process_batch(segments, max_retries=1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual([item["id"] for item in result["segments"]], ["seg_001", "seg_002", "seg_003"])
        self.assertEqual(result["failed_segments"][0]["segment_id"], "seg_002")


if __name__ == "__main__":
    unittest.main()
