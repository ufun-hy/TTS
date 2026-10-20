import copy
import json
from pathlib import Path
import random
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server.first_round_plan import build_plan, project_inputs, normalized_source
from server.first_round_preparation import FirstRoundPreparation
from server.prepared_live_session import PreparedLiveSession
from server.context_variants import validate_variant_result
from server import text_studio
from server.live_session_blocks import SynthesisBlockLiveSessionManager
from urllib.request import Request, urlopen


def project(complete=False):
    return {"project_id": "project-0001", "voice": "default", "paragraphs": [
        {"id": "p1", "candidates": ["甲" * 120 + "。", "乙" * 120 + "。"]},
        {"id": "p2", "candidates": ["丙" * 120 + "。", "丁" * 120 + "。"]},
        {"id": "p3", "candidates": ["最后介绍。"] if complete else []}]}


class FirstRoundPreparationTests(unittest.TestCase):
    def service(self, synthesize=lambda *_: b"RIFFdata", fingerprint=lambda _: "voice-v1"):
        root = Path(tempfile.mkdtemp(prefix="first-round-pipeline-"))
        service = FirstRoundPreparation(root, synthesize, fingerprint)
        self.addCleanup(service.close)
        return service, root

    def wait_ready(self, service):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = service.status("project-0001")
            if status["status"] in ("ready", "failed"):
                return status
            time.sleep(.01)
        self.fail(str(service.status("project-0001")))

    def test_batch_append_keeps_choices_and_never_flushes_unstable_tail(self):
        partial = build_plan(project(), "fp", rng=random.Random(5))
        complete = build_plan(project(True), "fp", partial, random.Random(9))
        self.assertFalse(partial["complete"])
        self.assertEqual(len(partial["blocks"]), 1)
        self.assertEqual(partial["blocks"][0], complete["blocks"][0])
        self.assertEqual(partial["choices"]["p1"], complete["choices"]["p1"])
        self.assertEqual(partial["choices"]["p2"], complete["choices"]["p2"])
        self.assertEqual(len(complete["blocks"]), 2)

    def test_incomplete_sentence_is_not_synthesized(self):
        p = project()
        p["paragraphs"][0]["candidates"] = ["这是长句的前半部分" * 30]
        p["paragraphs"][1]["candidates"] = []
        self.assertEqual(build_plan(p, "fp")["blocks"], [])

    def test_first_unfinished_unit_prevents_out_of_order_preparation(self):
        p = project(True)
        p["paragraphs"][0]["candidates"] = []
        plan = build_plan(p, "fp")
        self.assertEqual(plan["generated_units"], 0)
        self.assertEqual(plan["blocks"], [])

    def test_background_synthesis_overlaps_later_text_batches(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def synthesize(text, voice):
            calls.append(text)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return b"RIFFdata"
        service, _ = self.service(synthesize)
        try:
            service.update(project(), activate=True)
            self.assertTrue(entered.wait(1))
            before = copy.deepcopy(service._plans["project-0001"])
            status = service.update(project(True))
            self.assertEqual(status["total_segments"], 2)
            self.assertEqual(status["prepared_segments"], 0)
            self.assertFalse(release.is_set())
            release.set()
            self.assertEqual(self.wait_ready(service)["status"], "ready")
            self.assertEqual(calls[0], before["blocks"][0]["text"])
            self.assertEqual(len(calls), 2)
        finally:
            release.set()

    def test_prepared_plan_is_reused_at_live_start_without_new_random_selection(self):
        service, root = self.service()
        p = project(True)
        service.update(p, activate=True)
        self.assertEqual(self.wait_ready(service)["status"], "ready")
        units, groups, _, _ = project_inputs(p)
        pools = normalized_source(units, groups)
        plan = service.handoff(p["project_id"], pools, "default")
        self.assertIsNotNone(plan)
        sent = []
        def enqueue(*args):
            sent.append(args)
            if len(sent) == len(plan["blocks"]):
                session.stop()
        def forbidden_synthesis(*_):
            self.fail("ready first round must not be synthesized again")
        session = PreparedLiveSession("live-test", "default", [], forbidden_synthesis, enqueue, lambda _: {},
            candidate_pools=pools, first_round=plan, audio_root=root / "live")
        session.start()
        session._thread.join(1)
        self.assertFalse(session._thread.is_alive())
        self.assertEqual([s[2] for s in sent], [b["text"] for b in plan["blocks"]])
        self.assertEqual(session._previous_candidate_indexes, plan["candidate_indexes"])
        self.assertEqual([s[1] for s in sent], [1, 2])

    def test_text_edit_reuses_unchanged_block_and_rejects_old_request(self):
        calls = []
        service, _ = self.service(lambda text, _: calls.append(text) or b"RIFFdata")
        p = project(True)
        service.update(p, activate=True)
        self.wait_ready(service)
        old = normalized_source(*project_inputs(p)[:2])
        p["paragraphs"][-1]["candidates"] = ["新的结尾。"]
        service.update(p)
        self.wait_ready(service)
        self.assertEqual(len(calls), 3)
        self.assertIsNone(service.handoff(p["project_id"], old, "default"))

    def test_voice_change_invalidates_prepared_audio(self):
        version = ["one"]
        service, _ = self.service(fingerprint=lambda _: version[0])
        p = project(True)
        service.update(p, activate=True)
        self.wait_ready(service)
        source = normalized_source(*project_inputs(p)[:2])
        version[0] = "two"
        self.assertIsNone(service.handoff(p["project_id"], source, "default"))
        service.pause(p["project_id"])
        status = service.update(p)
        self.assertEqual(status["prepared_segments"], 0)

    def test_edit_during_synthesis_does_not_publish_obsolete_audio_as_ready(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def synthesize(text, _):
            calls.append(text)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return b"RIFFdata"
        service, _ = self.service(synthesize)
        p = project(True)
        service.update(p, activate=True)
        try:
            self.assertTrue(entered.wait(1))
            p["paragraphs"][0]["candidates"] = ["新" * 120 + "。"]
            service.pause(p["project_id"])
            service.update(p)
            release.set()
            deadline = time.monotonic() + 1
            while service._working and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(service.status(p["project_id"])["prepared_segments"], 0)
            self.assertEqual(len(calls), 1)
            service.update(p, activate=True)
            self.assertEqual(self.wait_ready(service)["status"], "ready")
            self.assertEqual(calls[1], "新" * 120 + "。")
        finally:
            release.set()

    def test_voice_file_change_during_synthesis_is_not_cached_under_old_fingerprint(self):
        version = ["one"]
        def synthesize(*_):
            version[0] = "two"
            return b"RIFFdata"
        service, _ = self.service(synthesize, fingerprint=lambda _: version[0])
        service.update(project(True), activate=True)
        status = self.wait_ready(service)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["prepared_segments"], 0)

    def test_pause_and_restart_keep_selection_but_do_not_auto_synthesize(self):
        service, root = self.service()
        p = project(True)
        service.update(p, activate=True)
        self.wait_ready(service)
        choices = service._plans[p["project_id"]]["choices"]
        service.pause(p["project_id"])
        replacement = FirstRoundPreparation(root, lambda *_: self.fail("must not auto restart"), lambda _: "voice-v1")
        self.addCleanup(replacement.close)
        self.assertFalse(replacement.status(p["project_id"])["enabled"])
        self.assertEqual(replacement._plans[p["project_id"]]["choices"], choices)
        self.assertEqual(replacement.status(p["project_id"])["prepared_segments"], 2)

    def test_prohibited_candidate_is_excluded_and_revocation_invalidates_plan(self):
        p = project(True)
        p["paragraphs"][0]["candidates"] = ["免费试吃。", "安全介绍。"]
        plan = build_plan(p, "fp")
        self.assertEqual(plan["choices"]["p1"]["text"], "安全介绍。")
        self.assertNotIn("免费试吃", "".join(b["text"] for b in plan["blocks"]))

    def test_complete_context_group_can_prepare_before_next_group(self):
        units = [{"id": "p1", "original_text": "第一句。"}, {"id": "p2", "original_text": "第二句。"}]
        response = validate_variant_result({"variants": [{"segments": [
            {"id": "p1", "text": "甲" * 130 + "。"}, {"id": "p2", "text": "乙" * 130 + "。"}]}]}, units, 1, "g1", 1)
        p = {"project_id": "project-0001", "voice": "default", "paragraphs": [
            *[{**u, **v} for u, v in zip(units, response["paragraphs"])],
            {"id": "p3", "original_text": "未完成。", "candidates": []}],
            "context_groups": [response["group"], {"id": "g2", "paragraph_ids": ["p3"], "variants": []}]}
        plan = build_plan(p, "fp")
        self.assertEqual(len(plan["blocks"]), 1)
        self.assertEqual(plan["variant_selection"][0]["variant_id"], "g1-r1-v1")

    def test_tts_error_preserves_text_and_can_resume(self):
        service, _ = self.service(lambda *_: (_ for _ in ()).throw(RuntimeError("tts unavailable")))
        service.update(project(True), activate=True)
        self.assertEqual(self.wait_ready(service)["error"], "tts unavailable")
        service.synthesize = lambda *_: b"RIFFdata"
        service.update(project(True), activate=True)
        self.assertEqual(self.wait_ready(service)["status"], "ready")

    def test_http_save_batches_prepare_in_background_and_live_adopts_plan(self):
        root = Path(tempfile.mkdtemp(prefix="preparation-http-"))
        synthesized, sent = [], []
        manager = SynthesisBlockLiveSessionManager("http://unused", "http://unused", tts_api_key="test",
            synthesize=lambda text, _: synthesized.append(text) or b"RIFFdata",
            enqueue=lambda *args: (sent.append(args), manager._session.stop() if len(sent) == 2 else None),
            cache_status=lambda _: {}, cache_cleanup=lambda _: {})
        with patch.object(text_studio, "build_live_manager", return_value=manager), \
             patch.object(text_studio.VoiceStore, "cache_fingerprint", return_value="fp"), \
             patch.object(text_studio.TTSClient, "synthesize", side_effect=lambda text, *a, **kw: (synthesized.append(text) or b"RIFFdata", 0)), \
             patch("server.prepared_live_session.PREPARED_AUDIO_ROOT", root / "live"):
            handler = text_studio.make_handler(root, "http://unused", tts_api_key="test")
            server = text_studio.StudioServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def post(path, data):
                req = Request(f"http://127.0.0.1:{server.server_port}" + path,
                    data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=3) as response:
                    return json.load(response)
            try:
                p = project()
                # New ids are assigned by the same project persistence API as the editor.
                p.pop("project_id")
                saved = post('/api/project/save', p)
                p['project_id'] = saved['project_id']
                post('/api/preparation/start', {'project_id': p['project_id']})
                deadline = time.monotonic() + 1
                while not synthesized and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(synthesized), 1)
                self.assertEqual(sent, [])
                p['paragraphs'][-1]['candidates'] = ['最后介绍。']
                post('/api/project/save', p)
                deadline = time.monotonic() + 1
                while handler.audio_preparation.status(p['project_id'])['status'] != 'ready' and time.monotonic() < deadline:
                    time.sleep(.01)
                prepared_text = list(synthesized)
                units, _, _, _ = project_inputs(p)
                result = post('/api/live/start', {'project_id': p['project_id'], 'voice': 'default', 'segments': units})
                self.assertTrue(result['preparation_reused'])
                manager._session._thread.join(1)
                self.assertEqual([item[2] for item in sent], prepared_text)
                self.assertEqual(synthesized, prepared_text)
            finally:
                handler.audio_preparation.close()
                if manager._session:
                    manager._session.stop()
                    manager._session._thread.join(1)
                server.shutdown()
                server.server_close()
                thread.join(1)


if __name__ == "__main__":
    unittest.main()
