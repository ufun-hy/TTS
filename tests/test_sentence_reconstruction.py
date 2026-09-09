import unittest

from timeline.sentence_reconstruction import ReconstructionConfig, reconstruct_segments


def segment(text, start, end, **extra):
    return {"id": extra.pop("id", f"s{start}"), "start": start, "end": end, "text": text, **extra}


class SentenceReconstructionTests(unittest.TestCase):
    def test_continuous_fragments_merge_without_semantic_judgment(self):
        output, report = reconstruct_segments([
            segment("姐妹们", 0, 1),
            segment("今天到手23块8", 1, 2),
            segment("直接拍", 2, 3),
        ])
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["text"], "姐妹们今天到手23块8直接拍")
        self.assertEqual(report["text_retention"], 1.0)

    def test_real_pause_creates_boundary(self):
        output, report = reconstruct_segments([
            segment("前面连续说话", 0, 4),
            segment("停顿后继续", 4.4, 6),
        ], ReconstructionConfig(pause_threshold=0.05))
        self.assertEqual(len(output), 2)
        self.assertEqual(report["pause_boundaries"], 1)

    def test_default_keeps_34_seconds_of_continuous_speech_together(self):
        output, report = reconstruct_segments([
            segment("第一段", 0, 12),
            segment("第二段", 12, 24),
            segment("第三段", 24, 34.04),
        ])
        self.assertEqual(len(output), 1)
        self.assertEqual(report["max_context_boundaries"], 0)
        self.assertAlmostEqual(report["max_context_duration"], 34.04)

    def test_safety_limit_can_still_create_technical_boundary(self):
        output, report = reconstruct_segments([
            segment("第一段", 0, 20),
            segment("第二段", 20, 40),
            segment("第三段", 40, 60),
        ], ReconstructionConfig(max_context_duration=45))
        self.assertEqual(len(output), 2)
        self.assertEqual(report["max_context_boundaries"], 1)
        self.assertEqual("".join(item["text"] for item in output), "第一段第二段第三段")

    def test_source_ids_are_preserved(self):
        output, _ = reconstruct_segments([
            segment("今天两件", 0, 1.4, id="asr_1"),
            segment("368", 1.4, 2.0, id="asr_2"),
        ])
        self.assertEqual(output[0]["source_segment_ids"], ["asr_1", "asr_2"])


if __name__ == "__main__":
    unittest.main()
