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


if __name__ == "__main__":
    unittest.main()
