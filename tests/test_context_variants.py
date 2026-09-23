import copy
import random
import unittest

from server.context_variants import (
    build_context_groups, build_variant_prompt, choose_variant_round,
    validate_context_project, validate_variant_result,
)


class ContextVariantTests(unittest.TestCase):
    def setUp(self):
        self.units = [{"id": f"p{i}", "original_text": text} for i, text in enumerate(
            ["这件外套是宽松版型。", "里面叠穿毛衣也可以。", "腰带可以调节。"], 1)]
        self.raw = {"variants": [{"segments": [{"id": p["id"], "text": f"版本{v}：{p['original_text']}"}
                                             for p in self.units]} for v in range(3)]}
        self.result = validate_variant_result(self.raw, self.units, 3, "g1", 1)
        self.project = [{**p, **r} for p, r in zip(self.units, self.result["paragraphs"])]

    def test_group_coverage_and_context_prompt(self):
        units = self.units * 20
        units = [{**p, "id": f"p{i}"} for i, p in enumerate(units)]
        groups = build_context_groups(units)
        self.assertGreater(len(groups), 1)
        self.assertEqual([i for g in groups for i in g["paragraph_ids"]], [p["id"] for p in units])
        validate_context_project(units, groups)
        prompt = build_variant_prompt(self.units, 3, "保持口语", "前文", "后文")
        for value in ("前文", "后文", "p1", "p2", "p3", "完整上下文"):
            self.assertIn(value, prompt)

    def test_complete_variants_and_group_selection(self):
        groups = [self.result["group"]]
        validate_context_project(self.project, groups, complete=True)
        previous = None
        for _ in range(30):
            segments, choices = choose_variant_round(self.project, groups, previous, random.Random(42))
            self.assertNotEqual(choices, previous)
            self.assertEqual(len({s["variant_id"] for s in segments}), 1)
            self.assertEqual([s["text"] for s in segments], [p["candidates"][choices[0]] for p in self.project])
            previous = choices

    def test_rejects_partial_duplicate_extra_reordered_and_empty_model_output(self):
        bad = []
        for change in (lambda s: s.pop(), lambda s: s.append(s[0]),
                       lambda s: s.__setitem__(1, s[0]), lambda s: s.reverse(),
                       lambda s: s[0].update(text="")):
            raw = copy.deepcopy(self.raw)
            change(raw["variants"][0]["segments"])
            bad.append(raw)
        bad.append({"variants": self.raw["variants"][:2]})
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_variant_result(raw, self.units, 3, "g1", 1)

    def test_slot_compaction_and_stale_source_cannot_start_live(self):
        project = copy.deepcopy(self.project)
        project[1]["candidates"].pop(0)
        with self.assertRaises(ValueError):
            validate_context_project(project, [self.result["group"]], complete=True)
        project = copy.deepcopy(self.project)
        project[0]["original_text"] += "改价。"
        validate_context_project(project, [self.result["group"]])  # saving edits is allowed
        with self.assertRaisesRegex(ValueError, "source changed"):
            validate_context_project(project, [self.result["group"]], complete=True)

    def test_overlap_or_reordered_groups_are_rejected(self):
        group = copy.deepcopy(self.result["group"])
        group["paragraph_ids"].reverse()
        with self.assertRaises(ValueError):
            validate_context_project(self.project, [group])
        with self.assertRaises(ValueError):
            validate_context_project(self.project, [self.result["group"], self.result["group"]])

    def test_unfinished_units_remain_intact(self):
        text = "如果您买两件，" + "材质" * 110 + "，"
        units = [{"id": "a", "original_text": text}, {"id": "b", "original_text": "就一起发货。"}]
        self.assertEqual(build_context_groups(units)[0]["paragraph_ids"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
