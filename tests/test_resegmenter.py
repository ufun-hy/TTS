import unittest

from timeline.resegmenter import ResegmentConfig, resegment, split_text


def segment(text, start, end, **extra):
    return {"id": extra.pop("id", f"s{start}"), "start": start, "end": end, "text": text, **extra}


class ResegmenterTests(unittest.TestCase):
    def test_continuous_asr_is_grouped_in_time_order(self):
        source = [
            segment("早点给你们去发走", 0, 1.2),
            segment("咱先打单子", 1.2, 2.28),
            segment("姐妹们", 2.28, 2.9),
            segment("咱先把单子直接打出来之后", 2.9, 5.26),
        ]
        children, report = resegment(source)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["text"], "".join(item["text"] for item in source))
        self.assertEqual(children[0]["pause_after"], 0.0)
        self.assertEqual(report["text_retention"], 1.0)

    def test_pause_is_preserved_exactly(self):
        source = [
            segment("特别是小孩", 26.02, 27.02),
            segment("咱家这个大甜桃", 30.0, 30.88),
        ]
        children, _ = resegment(source)
        self.assertEqual(len(children), 2)
        self.assertAlmostEqual(children[0]["speech_end"], 27.02)
        self.assertAlmostEqual(children[0]["pause_after"], 2.98)
        self.assertAlmostEqual(children[0]["end"], 30.0)

    def test_context_cut_does_not_insert_pause(self):
        source = [
            segment("A", 0, 6),
            segment("B", 6, 12),
            segment("C", 12, 18),
        ]
        children, _ = resegment(source, ResegmentConfig(max_context_duration=15))
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]["pause_after"], 0.0)
        self.assertEqual(children[0]["end"], children[0]["speech_end"])
        self.assertEqual(children[1]["start"], children[0]["speech_end"])

    def test_no_content_categories_are_emitted(self):
        source = [
            segment("姐妹们今天23块8直接拍", 0, 4),
            segment("继续讲桃子", 4, 5),
        ]
        children, _ = resegment(source)
        self.assertNotIn("semantic_type", children[0])
        self.assertNotIn("semantic_boundary", children[0])
        self.assertNotIn("boundary_source", children[0])

    def test_text_only_fallback_is_simple_size_chunking(self):
        text = "这是一段没有时间戳的文本" * 20
        pieces = split_text(text, ResegmentConfig(max_context_duration=5))
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), text)


if __name__ == "__main__":
    unittest.main()
