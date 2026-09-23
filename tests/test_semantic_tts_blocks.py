import unittest

from server.semantic_tts_blocks import continuous_text, semantic_blocks


class SemanticBlockTests(unittest.TestCase):
    def blocks(self, text):
        return semantic_blocks([{"id": "p1", "text": text}], filter_prohibited=False)

    def test_continuous_text_sources_and_no_artificial_newline(self):
        segments = [{"id": "a", "text": "整体比较宽松。", "group_id": "g1", "variant_id": "v1"},
                    {"id": "b", "text": "里面可以叠穿。", "group_id": "g1", "variant_id": "v1"}]
        blocks = semantic_blocks(segments)
        self.assertEqual(blocks[0]["text"], "整体比较宽松。里面可以叠穿。")
        self.assertEqual([s["paragraph_id"] for s in blocks[0]["sources"]], ["a", "b"])
        self.assertEqual(blocks[0]["sources"][1]["start"], len(segments[0]["text"]))

    def test_long_clause_is_cut_at_punctuation_before_limit(self):
        text = "甲" * 145 + "。" + "这款腰间采用的是可以自己自由调节的系带设计，" * 5
        blocks = self.blocks(text)
        self.assertEqual(blocks[0]["text"], "甲" * 145 + "。")
        self.assertEqual("".join(b["text"] for b in blocks), text)
        self.assertTrue(all(len(b["text"]) <= 200 for b in blocks))
        self.assertTrue(all(b["boundary"] != "hard_cut" for b in blocks))

    def test_natural_ending_after_target_and_short_tail(self):
        text = "甲" * 190 + "。" + "下一句。" * 8
        self.assertEqual(len(self.blocks(text)[0]["text"]), 191)
        self.assertEqual(self.blocks("甲" * 199 + "。")[0]["text"], "甲" * 199 + "。")

    def test_unpunctuated_hard_cut_is_explicit_and_lossless(self):
        text = "甲" * 431
        blocks = self.blocks(text)
        self.assertEqual([len(b["text"]) for b in blocks], [200, 200, 31])
        self.assertEqual([b["boundary"] for b in blocks], ["hard_cut", "hard_cut", "end"])

    def test_closing_quote_stays_with_sentence_and_numeric_comma_is_not_boundary(self):
        quoted = "她说：“" + "甲" * 170 + "。”"
        blocks = self.blocks(quoted + "下一句。" * 12)
        self.assertTrue(blocks[0]["text"].startswith(quoted))
        self.assertFalse(any(b["text"].startswith('”') for b in blocks))
        text = "甲" * 140 + "，" + "价格1,999.99元" + "乙" * 70
        blocks = self.blocks(text)
        self.assertEqual(blocks[0]["text"], "甲" * 140 + "，")

    def test_dynamic_token_never_splits(self):
        text = "甲" * 195 + "{{current_time}}" + "乙" * 25
        blocks = self.blocks(text)
        self.assertTrue(any("{{current_time}}" in b["text"] for b in blocks))
        self.assertEqual("".join(b["text"] for b in blocks), text)

    def test_english_word_boundary_between_units(self):
        text, _ = continuous_text([{"id": "a", "text": "hello"}, {"id": "b", "text": "world"}])
        self.assertEqual(text, "hello world")

    def test_prohibited_sentence_spanning_units_is_removed_with_mapping(self):
        blocks = semantic_blocks([{"id": "a", "text": "欢迎。免费"},
                                  {"id": "b", "text": "试吃。口感清甜。"}])
        self.assertEqual(blocks[0]["text"], "欢迎。口感清甜。")
        self.assertEqual([s["paragraph_id"] for s in blocks[0]["sources"]], ["a", "b"])
        self.assertEqual(blocks[0]["sources"][1]["start"], 3)


if __name__ == "__main__":
    unittest.main()
