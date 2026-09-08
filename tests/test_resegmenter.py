import unittest

from timeline.resegmenter import ResegmentConfig, resegment, split_text, validate_timeline


class ResegmenterTests(unittest.TestCase):
    def test_split_preserves_text_and_time_axis(self):
        source = [{"id": "seg_001", "start": 10, "end": 50, "text": "第一句说明商品。第二句说明价格和优惠。第三句说明售后。"}]
        children, report = resegment(source, ResegmentConfig(max_duration=5, preferred_max=4, min_duration=2))
        self.assertGreater(len(children), 1)
        self.assertEqual("".join(item["text"] for item in children), source[0]["text"])
        self.assertEqual(children[0]["start"], 10)
        self.assertEqual(children[-1]["end"], 50)
        self.assertEqual(report["text_retention"], 1.0)
        self.assertTrue(report["order_preserved"])

    def test_long_unpunctuated_text_has_stable_fallback_chunks(self):
        text = "这是一段没有标点但仍然需要被安全拆开的直播文本" * 8
        pieces = split_text(text, ResegmentConfig(max_duration=10, preferred_max=8, min_duration=3))
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), text)
        self.assertLessEqual(max(len(piece) for piece in pieces), 60)

    def test_word_timestamps_are_preferred(self):
        source = [{
            "id": "seg_002",
            "start": 0,
            "end": 4,
            "text": "先看商品。再看价格。",
            "words": [
                {"word": "先看商品。", "start": 0, "end": 1.5},
                {"word": "再看价格。", "start": 2, "end": 4},
            ],
        }]
        children, _ = resegment(source)
        self.assertEqual(children[0]["start"], 0)
        self.assertEqual(children[-1]["end"], 4)
        self.assertEqual("".join(item["text"] for item in children), source[0]["text"])

    def test_real_word_pause_is_preserved(self):
        source = [{
            "id": "seg_pause",
            "start": 0,
            "end": 3,
            "text": "先看商品。再看价格。",
            "words": [
                {"word": "先看商品。", "start": 0, "end": 1.2},
                {"word": "再看价格。", "start": 1.9, "end": 2.5},
            ],
        }]
        children, _ = resegment(source)
        self.assertAlmostEqual(children[0]["speech_end"], 1.2)
        self.assertAlmostEqual(children[0]["pause_after"], 0.7)
        self.assertAlmostEqual(children[0]["end"], 1.9)
        self.assertAlmostEqual(children[0]["speech_duration"] + children[0]["pause_after"], children[0]["timeline_duration"])

    def test_word_pause_between_asr_segments_is_preserved(self):
        source = [
            {"id": "seg_a", "start": 0, "end": 1, "text": "先看商品", "words": [{"word": "先看商品", "start": 0, "end": 0.8}]},
            {"id": "seg_b", "start": 1.7, "end": 2.5, "text": "再看价格", "words": [{"word": "再看价格", "start": 1.7, "end": 2.4}]},
        ]
        children, report = resegment(source)
        self.assertAlmostEqual(children[0]["pause_after"], 0.9)
        self.assertAlmostEqual(children[0]["end"], 1.7)
        self.assertEqual(report["segments_with_pause"], 2)
        self.assertTrue(report["order_preserved"])

    def test_semantic_feature_to_price_boundary(self):
        source = [{"id": "seg_semantic", "start": 0, "end": 8, "text": "这个结构用起来会更方便，今天直播间到手是199。"}]
        children, _ = resegment(source)
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]["semantic_boundary"], "feature_to_price")
        self.assertEqual("".join(item["text"] for item in children), source[0]["text"])

    def test_price_entity_stays_together(self):
        source = [{"id": "seg_entity", "start": 0, "end": 6, "text": "今天两件368，而且送一个赠品。"}]
        children, _ = resegment(source)
        self.assertIn("两件368", children[0]["text"])

    def test_old_timeline_fields_remain_compatible(self):
        source = [{"id": "seg_old", "start": 0, "end": 4, "text": "旧格式仍然可以运行。"}]
        children, _ = resegment(source)
        self.assertEqual(children[0]["pause_after"], 0.0)
        self.assertEqual(children[0]["speech_duration"], children[0]["timeline_duration"])


if __name__ == "__main__":
    unittest.main()
