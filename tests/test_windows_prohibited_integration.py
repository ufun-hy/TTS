"""Regression coverage for Windows Studio prohibited text persistence and TTS guard."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import text_studio
from server.prohibited_speech import RULES, analyze, filter_segments


class ProhibitedWindowsIntegrationTests(unittest.TestCase):
    def test_preview_filters_before_synthesis(self):
        sent = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self):
                return b"RIFFdemo"
        def open_request(req, timeout=0):
            sent.append(json.loads(req.data))
            return Response()
        with patch.object(text_studio, "_read_keychain_api_key", return_value="key"), \
             patch.object(text_studio.urllib.request, "urlopen", side_effect=open_request):
            audio = text_studio._tts_preview("免费试吃。口感清甜。", "default", "http://127.0.0.1:8765")
        self.assertEqual(audio, b"RIFFdemo")
        self.assertEqual(sent[0]["text"], "口感清甜。")

    def test_preview_all_blocked_never_calls_gateway(self):
        with patch.object(text_studio.urllib.request, "urlopen") as request:
            with self.assertRaisesRegex(ValueError, "没有可试听内容"):
                text_studio._tts_preview("免费试吃。", "default", "http://127.0.0.1:8765")
            request.assert_not_called()

    def test_review_is_exact_and_survives_project_save(self):
        original = "免费试吃。口感清甜。"
        hit = analyze(original)["blocked"][0]
        review = {
            "candidate_index": 0, "candidate_text": original,
            "start": hit["start"], "text": hit["text"],
            "labels": hit["labels"], "rules_version": RULES["version"],
            "confirmed_at": "2026-10-20T13:00:00Z",
        }
        payload = {
            "name": "Windows 禁用话术", "provider": "codex",
            "paragraphs": [{"id": "p0001", "original_text": original,
                            "candidates": [original], "prohibited_reviews": [review]}],
        }
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            stored = text_studio.save_project(root, payload)
            loaded = text_studio.load_project(root, stored["project_id"])
        self.assertEqual(loaded["paragraphs"][0]["prohibited_reviews"], [review])
        segments = filter_segments([{"id": "p0001", "candidates": [original],
                                     "prohibited_reviews": [review]}])
        self.assertEqual(segments[0]["candidates"], [original])
        changed = original.replace("清甜", "清香")
        self.assertEqual(filter_segments([{"id": "p0001", "candidates": [changed],
                                          "prohibited_reviews": [review]}])[0]["candidates"], ["口感清香。"])

    def test_invalid_review_rejected_by_project_save(self):
        with tempfile.TemporaryDirectory() as dirname:
            with self.assertRaises(ValueError):
                text_studio.save_project(Path(dirname), {"paragraphs": [
                    {"id": "p1", "prohibited_reviews": [{"candidate_index": -1}]}
                ]})


if __name__ == "__main__":
    unittest.main()
