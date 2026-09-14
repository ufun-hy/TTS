import unittest

from voice_datasets.prompt import COSYVOICE3_PROMPT_PREFIX, build_zero_shot_prompt_text


class VoicePromptTests(unittest.TestCase):
    def test_raw_reference_gets_cosyvoice3_boundary(self):
        text = "希望你以后能够做的比我还好呦。"
        self.assertEqual(build_zero_shot_prompt_text(text), COSYVOICE3_PROMPT_PREFIX + text)

    def test_existing_valid_prefix_is_idempotent(self):
        text = "测试参考文本。"
        prepared = COSYVOICE3_PROMPT_PREFIX + text
        self.assertEqual(build_zero_shot_prompt_text(prepared), prepared)

    def test_reference_cannot_smuggle_prompt_control_token(self):
        with self.assertRaises(ValueError):
            build_zero_shot_prompt_text("前半段<|endofprompt|>后半段")

    def test_empty_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            build_zero_shot_prompt_text("   ")


if __name__ == "__main__":
    unittest.main()
