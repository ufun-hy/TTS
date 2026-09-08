import tempfile
import unittest
from pathlib import Path

from timeline.cache import AudioCache
from timeline.player import Player
from timeline.runtime_selector import RuntimeSelector
from timeline.scheduler import LookaheadScheduler
from timeline.session import RuntimeSession


def twenty_segments():
    values = []
    for index in range(20):
        segment_id = f"seg_{index + 1:03d}"
        if index % 2:
            values.append({
                "id": segment_id,
                "start": index * 2,
                "end": index * 2 + 2,
                "duration_target": 2,
                "mode": "atomic",
                "variants": [f"第{index + 1}段表达一", f"第{index + 1}段表达二", f"第{index + 1}段表达三"],
            })
        else:
            values.append({
                "id": segment_id,
                "start": index * 2,
                "end": index * 2 + 2,
                "duration_target": 2,
                "mode": "composable",
                "slots": [
                    {"id": "opening", "required": True, "variants": [f"开始{index + 1}A", f"开始{index + 1}B"]},
                    {"id": "cta", "required": False, "variants": ["，大家可以看一下。", "，需要的朋友可以了解。"]},
                ],
            })
    return {"timeline_id": "runtime-test", "segments": values}


class RuntimeTests(unittest.TestCase):
    def test_composable_prefers_precomposed_candidates(self):
        segment = {
            "id": "candidate",
            "start": 0,
            "end": 10,
            "speech_duration": 10,
            "mode": "composable",
            "candidates": ["完整候选表达"],
            "slots": [{"id": "opening", "required": True, "variants": ["碎片"]}],
        }
        selection = RuntimeSelector(seed=1).select(segment, RuntimeSession("candidate-session", "candidate"))
        self.assertEqual(selection.final_text, "完整候选表达")
        self.assertEqual(selection.variant_ids, ["candidate_1"])

    def run_dry(self, root, seed):
        timeline = twenty_segments()
        session = RuntimeSession(f"session-{seed}", "runtime-test")
        scheduler = LookaheadScheduler(
            timeline,
            session,
            root / f"session-{seed}.json",
            root / f"report-{seed}.json",
            RuntimeSelector(seed=seed),
            AudioCache(root / "audio-cache", "runtime-test", f"session-{seed}"),
            None,
            Player(dry_run=True),
            "default",
            lookahead=3,
            dry_run=True,
        )
        return scheduler.run(), session

    def test_twenty_segments_keep_order_without_underrun(self):
        with tempfile.TemporaryDirectory() as directory:
            report, session = self.run_dry(Path(directory), 1)
            self.assertEqual(report["played_count"], 20)
            self.assertEqual(report["buffer_underrun_count"], 0)
            self.assertEqual([item["segment_id"] for item in session.played_segments], [f"seg_{i:03d}" for i in range(1, 21)])

    def test_two_sessions_change_runtime_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            _, first = self.run_dry(Path(directory), 1)
            _, second = self.run_dry(Path(directory), 2)
            first_text = [item["final_text"] for item in first.played_segments]
            second_text = [item["final_text"] for item in second.played_segments]
            self.assertNotEqual(first_text, second_text)


if __name__ == "__main__":
    unittest.main()
