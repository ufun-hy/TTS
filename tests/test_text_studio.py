import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import text_studio


class TextStudioTest(unittest.TestCase):
    def test_live_control_uses_candidate_pools_without_second_text_source(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "text-studio.html").read_text(encoding="utf-8")
        self.assertNotIn('id="liveText"', html)
        self.assertIn("liveCandidatePools", html)
        self.assertIn("开始循环智播", html)
        self.assertIn("candidates", html)
        self.assertIn("p.candidates[ci]=value", html)
        self.assertIn('id="livePlaybackSpeed"', html)
        self.assertIn('id="liveVolume"', html)
        self.assertIn("playback_speed", html)

    def test_diagnostics_preserve_success_schema_failure_and_timeout(self):
        cases = [
            (text_studio.subprocess.CompletedProcess([], 0,
             '{"paragraphs":[{"id":"p0009","candidates":["改写"]}]}',
             'model: test-model\nprovider: openai\n'), None),
            (text_studio.subprocess.CompletedProcess([], 0, '{"paragraphs":[]}', ''), ValueError),
            (text_studio.subprocess.TimeoutExpired('codex', 240, output=b'partial',
             stderr=b'model: test-model\n'), text_studio.subprocess.TimeoutExpired),
        ]
        # Retain diagnostic artifacts; repository cleanup requires explicit authorization.
        root = Path(tempfile.mkdtemp(prefix="text-studio-diagnostic-test-"))
        for index, (output, error) in enumerate(cases):
            target = root / str(index)
            kwargs = {"side_effect": output} if error is text_studio.subprocess.TimeoutExpired else {"return_value": output}
            with mock.patch.object(text_studio.shutil, "which", return_value="codex"), mock.patch.object(text_studio.subprocess, "run", **kwargs):
                def run():
                    return text_studio.generalize_paragraphs(
                        [{"id": "p0009", "original_text": "原稿"}], "codex", 1, "",
                        diagnostic_root=target, project_id="project-test", paragraph_indexes=[8])
                if error:
                    with self.assertRaises(error):
                        run()
                else:
                    self.assertEqual(run()[0]["candidates"], ["改写"])
            record = json.loads(next((target / "logs").glob("*.json")).read_text())
            self.assertEqual(record["status"], "failed" if error else "success")
            self.assertEqual(record["paragraph_start"], 8)
            self.assertEqual(record["batch_id"], "batch-002")
            self.assertTrue(record["end_time"])
            self.assertEqual(Path(record["response_path"]).read_text(),
                             "partial" if error is text_studio.subprocess.TimeoutExpired else output.stdout)
            if error is not ValueError:
                self.assertEqual(record["model"], "test-model")

    def test_split_paragraphs_preserves_source_paragraph_mapping(self):
        data = text_studio.split_paragraphs("第一句。第二句？\n\n第三段！")
        self.assertEqual([item["id"] for item in data], ["p0001", "p0002", "p0003"])
        self.assertEqual([item["original_text"] for item in data], ["第一句。", "第二句？", "第三段！"])
        self.assertEqual(data[0]["sentences"], ["第一句。"])
        self.assertEqual([item["source_paragraph_index"] for item in data], [1, 1, 2])

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
                "playback_speed": 1.05,
                "volume": 80,
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
            self.assertEqual(loaded["playback_speed"], 1.05)
            self.assertEqual(loaded["volume"], 80.0)
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
