import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import text_studio
from server.context_variants import build_context_groups, prepare_context_live, validate_variant_result
from server.live_session import LiveSessionManager
from server.live_session_blocks import SynthesisBlockLiveSessionManager


def project():
    units = [{"id": "p1", "original_text": "版型宽松。"}, {"id": "p2", "original_text": "可以叠穿。"}]
    raw = {"variants": [{"segments": [{"id": "p1", "text": "甲" * 120 + "。"},
                                     {"id": "p2", "text": "甲版可以叠穿。"}]},
                        {"segments": [{"id": "p1", "text": "乙" * 120 + "。"},
                                     {"id": "p2", "text": "乙版可以叠穿。"}]}]}
    result = validate_variant_result(raw, units, 2, "g1", 1)
    return {"paragraphs": [{**u, **p} for u, p in zip(units, result["paragraphs"])],
            "context_groups": [result["group"]]}


class ContextLiveTests(unittest.TestCase):
    def test_both_live_entrypoints_select_whole_variants_without_newlines(self):
        for manager_type in (LiveSessionManager, SynthesisBlockLiveSessionManager):
            with self.subTest(manager=manager_type.__name__):
                calls, enqueued = [], []
                def enqueue(*args):
                    enqueued.append(args)
                    if len(enqueued) == 2:
                        manager.stop()
                manager = manager_type("http://unused", "http://unused", tts_api_key="test",
                    synthesize=lambda text, voice: calls.append(text) or b"RIFF", enqueue=enqueue,
                    cache_status=lambda _: {"client_connected": True})
                payload = project()
                manager.start("default", payload)
                payload["paragraphs"][0]["candidates"][0] = "外部修改不应影响会话。"
                deadline = time.monotonic() + 2
                while len(enqueued) < 2 and time.monotonic() < deadline:
                    time.sleep(.01)
                manager.stop()
                self.assertEqual(len(calls), 2)
                self.assertNotEqual(calls[0], calls[1])
                self.assertTrue(all(('甲' in t) != ('乙' in t) for t in calls))
                self.assertTrue(all('\n' not in t and len(t) <= 200 for t in calls))
                self.assertEqual([args[1] for args in enqueued], [1, 2])
                self.assertIn("variant_id", manager.status()["variant_selection"][0])

    def test_filtering_never_reindexes_candidates_or_silently_drops_a_unit(self):
        payload = project()
        payload["paragraphs"][0]["candidates"][0] = "免费试吃。"
        original = copy.deepcopy(payload)
        with self.assertRaisesRegex(ValueError, "fully blocked"):
            prepare_context_live(payload)
        self.assertEqual(payload, original)

    def test_project_roundtrip_and_old_editor_cannot_discard_groups(self):
        root = Path(tempfile.mkdtemp(prefix="context-project-test-"))
        saved = text_studio.save_project(root, project())
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(text_studio.load_project(root, saved["project_id"])["context_groups"], saved["context_groups"])
        with self.assertRaisesRegex(ValueError, "group data is required"):
            text_studio.save_project(root, {"project_id": saved["project_id"], "paragraphs": saved["paragraphs"]})
        self.assertEqual(text_studio.load_project(root, saved["project_id"]), saved)

    def test_http_group_generation_and_live_contract(self):
        root = Path(tempfile.mkdtemp(prefix="context-api-test-"))
        live = mock.Mock()
        live.start.return_value = {"status": "starting"}
        with mock.patch.object(text_studio, "build_live_manager", return_value=live):
            server = text_studio.StudioServer(("127.0.0.1", 0), text_studio.make_handler(root, "http://unused"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(path, payload):
            req = Request(f"http://127.0.0.1:{server.server_port}{path}",
                          data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urlopen(req) as response:
                return json.load(response)
        try:
            payload = project()
            groups = post("/api/context/groups", {"paragraphs": payload["paragraphs"]})["context_groups"]
            self.assertEqual(groups, build_context_groups(payload["paragraphs"]))
            request = {"paragraphs": payload["paragraphs"], "group_id": "g1", "revision": 1, "candidate_count": 2}
            raw = {"variants": [{"segments": [{"id": p["id"], "text": p["candidates"][i]} for p in payload["paragraphs"]]} for i in range(2)]}
            with mock.patch.object(text_studio, "_run_provider", return_value=raw):
                generated = post("/api/context/generalize", request)
                self.assertEqual(generated["group"]["revision"], 2)
            polish_units = [{"id": p["id"], "original_text": p["candidates"][0]} for p in payload["paragraphs"]]
            polished = {"variants": [{"segments": [{"id": "p1", "text": "版型宽松，可以放心叠穿。"},
                                                     {"id": "p2", "text": polish_units[1]["original_text"]}]}]}
            with mock.patch.object(text_studio, "_run_provider", return_value=polished):
                response = post("/api/context/polish", {**request, "paragraphs": polish_units, "target_ids": ["p1"]})
                self.assertEqual(response["paragraphs"][1]["candidates"], [polish_units[1]["original_text"]])
                polished["variants"][0]["segments"][1]["text"] = "模型改动了不该改的单元。"
                with self.assertRaises(HTTPError) as caught:
                    post("/api/context/polish", {**request, "paragraphs": polish_units, "target_ids": ["p1"]})
                self.assertEqual(caught.exception.code, 400)
            post("/api/live/start", {"segments": payload["paragraphs"], "context_groups": payload["context_groups"]})
            self.assertEqual(live.start.call_args.args[1], payload)
            payload["paragraphs"][1]["candidates"].pop()
            with self.assertRaises(HTTPError) as caught:
                post("/api/live/start", {"segments": payload["paragraphs"], "context_groups": payload["context_groups"]})
            self.assertEqual(caught.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
