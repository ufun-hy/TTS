"""Standalone migrated text-safety policy tests (no Studio integration yet)."""
import unittest
from server.prohibited_speech import broadcast_text, filter_segments, analyze

class StandaloneProhibitedTests(unittest.TestCase):
    def test_sentence_exclusion_and_retained_source(self):
        text = "今天新鲜采摘。免费试吃。大家可以了解一下。"
        self.assertEqual(broadcast_text(text), "今天新鲜采摘。大家可以了解一下。")
        self.assertEqual(analyze(text)["blocked"][0]["text"], "免费试吃。")

    def test_filter_keeps_safe_candidates(self):
        original = [{"id": "p1", "candidates": ["免费试吃。", "口感清甜。"]}]
        filtered = filter_segments(original)
        self.assertEqual(filtered[0]["candidates"], ["口感清甜。"])
        self.assertEqual(original[0]["candidates"], ["免费试吃。", "口感清甜。"])

    def test_all_blocked_fails_closed(self):
        with self.assertRaises(ValueError):
            filter_segments([{"id": "p1", "text": "免费试吃。"}])

if __name__ == "__main__":
    unittest.main()
