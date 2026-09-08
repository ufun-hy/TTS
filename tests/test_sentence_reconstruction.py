import unittest

from timeline.sentence_reconstruction import reconstruct_segments
from timeline.resegmenter import resegment


def segment(text, start, end, **extra):
    return {"id": extra.pop("id", f"s{start}"), "start": start, "end": end, "text": text, "words": [{"word": text, "start": start, "end": end}], **extra}


class SentenceReconstructionTests(unittest.TestCase):
    def test_short_continuation_is_merged(self):
        output, report = reconstruct_segments([
            segment("大家看一下。", 0, 1.5),
            segment("这个桃子真的很甜。", 1.5, 3.7),
            segment("皮薄肉多。", 3.7, 5.0),
        ])
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["text"], "大家看一下。这个桃子真的很甜。皮薄肉多。")
        self.assertEqual(report["text_retention"], 1.0)

    def test_price_boundary_is_kept(self):
        output, _ = reconstruct_segments([
            segment("这个桃子汁水很多。", 0, 4.5),
            segment("今天到手23块8。", 4.5, 6.5),
        ])
        self.assertEqual(len(output), 2)
        self.assertIn("semantic_rule", output[0]["reconstruction_source"])

    def test_cta_and_interaction_can_merge(self):
        output, _ = reconstruct_segments([
            segment("想要的直接拍。", 0, 2.0),
            segment("现在就点链接。", 2.0, 3.5),
            segment("有没有喜欢吃桃子的？", 3.5, 5.5),
            segment("评论区告诉我。", 5.5, 7.0),
        ])
        self.assertLessEqual(len(output), 2)
        self.assertEqual("".join(item["text"] for item in output), "想要的直接拍。现在就点链接。有没有喜欢吃桃子的？评论区告诉我。")

    def test_long_input_is_split_at_max_duration(self):
        output, report = resegment([segment("很长的直播内容" * 20, 0, 26)])
        self.assertGreater(len(output), 1)
        self.assertEqual(report["segments_gt_25s"], 0)

    def test_large_pause_is_not_merged(self):
        output, _ = reconstruct_segments([
            segment("前一句内容", 0, 4),
            segment("后一句内容", 5.8, 7.8),
        ])
        self.assertEqual(len(output), 2)

    def test_entity_and_parent_trace_are_preserved(self):
        output, _ = reconstruct_segments([
            segment("今天两件", 0, 1.4, id="asr_1"),
            segment("368", 1.4, 2.0, id="asr_2"),
        ])
        self.assertEqual(output[0]["text"], "今天两件368")
        self.assertEqual(output[0]["source_segment_ids"], ["asr_1", "asr_2"])

    def test_resegment_output_has_natural_length_report(self):
        source = [segment(f"第{i}段直播内容", i * 1.0, (i + 1) * 1.0) for i in range(20)]
        children, report = resegment(source)
        self.assertEqual(report["reconstruction_text_retention"], 1.0)
        self.assertEqual("".join(item["text"] for item in children), "".join(item["text"] for item in source))
        self.assertEqual(report["segments_gt_25s"], 0)


if __name__ == "__main__":
    unittest.main()
