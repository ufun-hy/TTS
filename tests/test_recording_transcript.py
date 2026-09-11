import tempfile
import unittest
from pathlib import Path

from recording_transcript.cleaner import clean_transcript
from server.recording_transcript import TranscriptJob, _safe_filename


class RecordingTranscriptTests(unittest.TestCase):
    def test_sample_is_lightly_cleaned_into_readable_paragraphs(self):
        raw = "嗯今天呢我们主要给大家介绍一下这个产品然后这个产品呢最大的一个特点其实就是操作比较简单然后大家使用起来基本没有什么学习成本"
        self.assertEqual(clean_transcript(raw), "今天我们主要给大家介绍一下这个产品。\n\n这个产品最大的特点是操作比较简单，大家使用起来基本没有什么学习成本。")

    def test_removes_obvious_stutter_but_keeps_content_word_emphasis(self):
        self.assertEqual(clean_transcript("我我今天主要主要介绍这个这个产品"), "我今天主要介绍这个产品。")
        self.assertEqual(clean_transcript("这个东西好吃好吃"), "这个东西好吃好吃。")

    def test_segment_pause_creates_paragraph_without_exposing_timing(self):
        segments = [
            {"text": "今天介绍这个产品", "start": 0, "end": 2},
            {"text": "这个产品操作简单", "start": 2, "end": 4},
            {"text": "大家使用起来很方便", "start": 6, "end": 7},
        ]
        output = clean_transcript("".join(item["text"] for item in segments), segments)
        self.assertEqual(output, "今天介绍这个产品这个产品操作简单。\n\n大家使用起来很方便。")
        self.assertNotIn("start", output)
        self.assertNotIn("end", output)

    def test_upload_filename_is_restricted_to_supported_extensions(self):
        self.assertEqual(_safe_filename("%E6%96%B0%E5%BD%95%E9%9F%B3.MP3"), "新录音.MP3")
        with self.assertRaises(ValueError):
            _safe_filename("recording.txt")

    def test_public_job_result_only_contains_final_text(self):
        with tempfile.TemporaryDirectory() as directory:
            job = TranscriptJob("a" * 32, "recording.mp3", 3, Path(directory) / "input.mp3", stage="completed", text="最终文稿。")
            public = job.public()
        self.assertEqual(public["text"], "最终文稿。")
        self.assertNotIn("raw_text", public)
        self.assertNotIn("upload_path", public)


if __name__ == "__main__":
    unittest.main()
