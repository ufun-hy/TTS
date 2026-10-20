"""Windows context-group persistence and generation contract tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server import text_studio
from server.context_variants import build_context_groups

class WindowsContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.paragraphs=[{"id":"p1","original_text":"外套版型宽松。"},{"id":"p2","original_text":"腰带长度可调节。"}]
        self.groups=build_context_groups(self.paragraphs)

    def test_project_roundtrip_and_reject_missing_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            project=text_studio.save_project(root,{"paragraphs":self.paragraphs,"context_groups":self.groups})
            self.assertEqual(text_studio.load_project(root,project["project_id"])["context_groups"],self.groups)
            with self.assertRaisesRegex(ValueError,"context group data"):
                text_studio.save_project(root,{"project_id":project["project_id"],"paragraphs":self.paragraphs})

    def test_joint_generation_exact_coverage(self):
        def provider(_name,prompt,*_args,**_kwargs):
            self.assertIn("完整上下文",prompt)
            return {"variants":[{"segments":[{"id":"p1","text":"宽松的外套。"},{"id":"p2","text":"腰带可自行调节。"}]},
                                {"segments":[{"id":"p1","text":"这件外套宽松。"},{"id":"p2","text":"搭配可调腰带。"}]}]}
        with patch.object(text_studio,"_run_provider",side_effect=provider):
            result=text_studio.generalize_context_group_windows(
                {"paragraphs":self.paragraphs,"group_id":self.groups[0]["id"],
                 "revision":0,"candidate_count":2},Path("."))
        self.assertEqual(len(result["group"]["variants"]),2)
        self.assertEqual(result["paragraphs"][0]["candidates"],["宽松的外套。","这件外套宽松。"])

    def test_partial_group_is_rejected(self):
        with patch.object(text_studio,"_run_provider",return_value={"variants":[{"segments":[{"id":"p1","text":"不完整"}]}]}):
            with self.assertRaises(ValueError):
                text_studio.generalize_context_group_windows(
                    {"paragraphs":self.paragraphs,"group_id":"g0001","candidate_count":1},Path("."))

if __name__=="__main__":
    unittest.main()
