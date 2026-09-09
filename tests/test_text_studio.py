import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "text_studio.py"
spec = importlib.util.spec_from_file_location("text_studio", MODULE_PATH)
text_studio = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(text_studio)


class TextStudioTest(unittest.TestCase):
    def test_split_paragraphs_preserves_natural_paragraphs(self):
        data = text_studio.split_paragraphs("第一句。第二句？\n\n第三段！")
        self.assertEqual([item["id"] for item in data], ["p0001", "p0002"])
        self.assertEqual(data[0]["original_text"], "第一句。第二句？")
        self.assertEqual(data[0]["sentences"], ["第一句。", "第二句？"])

    def test_extract_json_from_fenced_output(self):
        raw = '```json\n{"paragraphs":[{"id":"p0001","candidates":["A"]}]}\n```'
        parsed = text_studio._extract_json(raw)
        self.assertEqual(parsed["paragraphs"][0]["id"], "p0001")

    def test_validate_result_keeps_request_order(self):
        raw = {
            "paragraphs": [
                {"id": "p0002", "candidates": ["B"]},
                {"id": "p0001", "candidates": ["A"]},
            ]
        }
        result = text_studio._validate_model_result(raw, ["p0001", "p0002"])
        self.assertEqual([item["id"] for item in result], ["p0001", "p0002"])

    def test_prompt_contains_no_placeholder_candidate_examples(self):
        prompt = text_studio._build_prompt(
            [{"id": "p0001", "original_text": "拍一单试吃一个。"}],
            3,
            "",
        )
        self.assertNotIn("版本1", prompt)
        self.assertNotIn("候选A", prompt)
        self.assertIn("事实必须保持不变", prompt)

    def test_long_script_is_processed_in_batches(self):
        paragraphs = [
            {"id": f"p{index:04d}", "original_text": f"第{index}段直播话术。"}
            for index in range(1, 122)
        ]

        def fake_run_provider(provider, prompt, timeout_seconds):
            payload = json.loads(prompt.split("输入：\n", 1)[1])
            return {
                "paragraphs": [
                    {"id": item["id"], "candidates": [item["text"]]}
                    for item in payload["paragraphs"]
                ]
            }

        with mock.patch.object(text_studio, "_run_provider", side_effect=fake_run_provider) as runner:
            result = text_studio.generalize_paragraphs(
                paragraphs,
                provider="codex",
                candidate_count=1,
                instruction="",
                batch_size=8,
            )

        self.assertEqual(len(result), 121)
        self.assertEqual(runner.call_count, 16)

    def test_project_round_trip_persists_candidates_and_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = text_studio.save_project(root, {
                "name": "桃子直播稿",
                "source_name": "桃子.txt",
                "source_text": "第一段。\n\n第二段。",
                "provider": "codex",
                "candidate_count": 3,
                "voice": "default",
                "instruction": "保持直播口语",
                "paragraphs": [
                    {
                        "id": "p0001",
                        "index": 1,
                        "original_text": "第一段。",
                        "sentences": ["第一段。"],
                        "candidates": ["泛化一", "泛化二", "泛化三"],
                        "selectedIndex": 1,
                        "editedText": "泛化二",
                    },
                    {
                        "id": "p0002",
                        "index": 2,
                        "original_text": "第二段。",
                        "sentences": ["第二段。"],
                        "candidates": [],
                        "selectedIndex": 0,
                        "editedText": "",
                    },
                ],
            })

            project_id = project["project_id"]
            loaded = text_studio.load_project(root, project_id)
            summaries = text_studio.list_projects(root)

            self.assertEqual(loaded["name"], "桃子直播稿")
            self.assertEqual(loaded["paragraphs"][0]["candidates"][1], "泛化二")
            self.assertEqual(loaded["paragraphs"][0]["selectedIndex"], 1)
            self.assertEqual(summaries[0]["project_id"], project_id)
            self.assertEqual(summaries[0]["paragraph_count"], 2)
            self.assertEqual(summaries[0]["generated_count"], 1)

    def test_project_update_reuses_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = text_studio.save_project(root, {
                "name": "测试",
                "source_name": "test.txt",
                "source_text": "第一段。",
                "provider": "codex",
                "candidate_count": 3,
                "voice": "default",
                "instruction": "",
                "paragraphs": [],
            })
            second = text_studio.save_project(root, {
                "project_id": first["project_id"],
                "name": "测试更新",
                "source_name": "test.txt",
                "source_text": "第一段。",
                "provider": "codex",
                "candidate_count": 3,
                "voice": "default",
                "instruction": "",
                "paragraphs": [],
            })

            self.assertEqual(first["project_id"], second["project_id"])
            self.assertEqual(first["created_at"], second["created_at"])
            self.assertEqual(text_studio.load_project(root, first["project_id"])["name"], "测试更新")


if __name__ == "__main__":
    unittest.main()
