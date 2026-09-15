import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

from audio_client.playback import PlaybackController
from local_runtime import FileGpuLease, RuntimeBusyError, RuntimeManager, RuntimeStage, UpdateError, validate_manifest
from server import tts_gateway
from server.engine_runtime import ManagedEngine
from server import text_studio_http_providers as providers
from server.live_session_blocks import SynthesisBlockLiveSessionManager
from server import text_studio
from server import recording_transcript
from server.recording_transcript import TranscriptJob, TranscriptJobStore
from recording_transcript.windows_chunks import split_wav
from recording_transcript import qwen_asr_windows


def _item(root: Path, item_id: str, sequence: int, session: str, status: str = "cached", duration: float = 20.0) -> None:
    (root / f"{item_id}.wav").write_bytes(b"RIFF")
    (root / f"{item_id}.json").write_text(json.dumps({
        "status": "completed", "sequence": sequence, "duration": duration,
        "playback_status": status, "server_metadata": {"session_id": session, "sequence": sequence},
    }), encoding="utf-8")


class RuntimeTests(unittest.TestCase):
    def test_one_owner_and_failed_release_keeps_owner(self):
        released = False
        def release():
            return released
        runtime = RuntimeManager(release)
        lease = runtime.acquire(RuntimeStage.REWRITE, "ollama", "qwen3:8b")
        with self.assertRaises(RuntimeBusyError):
            runtime.acquire(RuntimeStage.LIVE, "tts", "CosyVoice3")
        self.assertFalse(runtime.release(lease))
        self.assertEqual(runtime.snapshot()["state"], "ERROR")
        self.assertEqual(runtime.snapshot()["gpu_owner"], "ollama")

        released = True
        self.assertTrue(runtime.release(lease))
        self.assertEqual(runtime.snapshot()["state"], "IDLE")
        self.assertTrue(runtime.snapshot()["model_released"])

    def test_context_manager_releases_on_exception(self):
        runtime = RuntimeManager()
        with self.assertRaisesRegex(ValueError, "bad"):
            with runtime.operation(RuntimeStage.ASR, "asr", "Qwen3-ASR"):
                raise ValueError("bad")
        self.assertEqual(runtime.snapshot()["state"], "ERROR")
        self.assertEqual(runtime.snapshot()["last_error"], "bad")

    def test_tts_stop_failed_keeps_owner_and_blocks_other_gpu_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpu-owner.json"
            runtime = RuntimeManager(lock_path=str(path))
            lease = runtime.acquire(RuntimeStage.TTS_PREPARING, "tts", "CosyVoice3")
            self.assertFalse(runtime.release(lease, "ManagedEngine stop_failed"))
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot["state"], "ERROR")
            self.assertEqual(snapshot["gpu_owner"], "tts")
            self.assertFalse(snapshot["model_released"])
            self.assertEqual(FileGpuLease.read(path)["owner"], "tts")
            with self.assertRaises(RuntimeBusyError):
                RuntimeManager(lock_path=str(path)).acquire(RuntimeStage.ASR, "asr", "Qwen3-ASR")

    def test_file_lease_blocks_a_second_process_marker_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpu-owner.json"
            first = FileGpuLease(path, "asr", "Qwen3-ASR", "ASR")
            second = FileGpuLease(path, "ollama", "qwen3:8b", "REWRITE")
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertEqual(FileGpuLease.read(path)["owner"], "asr")
            self.assertTrue(first.release())
            self.assertTrue(second.acquire())
            self.assertTrue(second.release())

    def test_update_manifest_requires_https_and_valid_sha256(self):
        manifest = {
            "version": "1.2.3", "release_notes": "修复", "installer_url": "https://example.invalid/a.exe",
            "sha256": "a" * 64, "mandatory": False,
        }
        self.assertEqual(validate_manifest(manifest).version, "1.2.3")
        manifest["installer_url"] = "http://example.invalid/a.exe"
        with self.assertRaises(UpdateError):
            validate_manifest(manifest)

    def test_live_manager_holds_runtime_lease_until_worker_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeManager(lock_path=str(Path(directory) / "gpu-owner.json"))
            entered = threading.Event()
            release = threading.Event()
            manager = SynthesisBlockLiveSessionManager(
                "http://gateway", "http://cache", runtime=runtime,
                synthesize=lambda _text, _voice: (entered.set(), release.wait(1), b"RIFF")[2],
                enqueue=lambda *_args: None,
                cache_status=lambda _session: {"ready": 0, "client_connected": True},
                cache_cleanup=lambda _session: {},
            )
            manager.start("default", [{"id": "p1", "text": "一段话"}])
            self.assertTrue(entered.wait(1))
            self.assertEqual(runtime.snapshot()["state"], "LIVE")
            release.set()
            deadline = time.time() + 1
            while manager.status()["status"] in ("starting", "running") and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(runtime.snapshot()["state"], "IDLE")

    def test_live_tts_borrows_runtime_owner_and_does_not_release_it_early(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpu-owner.json"
            runtime = RuntimeManager(lock_path=str(path))
            runtime_lease = runtime.acquire(RuntimeStage.LIVE, "live-session", "CosyVoice3")
            state = {"ready": False}

            def launch():
                state["ready"] = True
                return type("Process", (), {
                    "poll": lambda self: None if state["ready"] else 0,
                    "terminate": lambda self: state.__setitem__("ready", False),
                    "wait": lambda self, timeout=None: 0,
                    "kill": lambda self: state.__setitem__("ready", False),
                })()

            engine = ManagedEngine(
                "http://engine",
                launcher=launch,
                probe=lambda: state["ready"],
                gpu_lease=FileGpuLease(path, "tts", "CosyVoice3", "TTS_PREPARING"),
            )
            try:
                engine.run(lambda _restarted: None)
                self.assertEqual(FileGpuLease.read(path)["owner"], "live-session")
                self.assertTrue(engine.stop_now())
                self.assertEqual(FileGpuLease.read(path)["owner"], "live-session")
                self.assertTrue(runtime.release(runtime_lease))
                self.assertEqual(FileGpuLease.read(path), {})
            finally:
                engine.close()

    def test_runtime_status_surfaces_gateway_stop_failure_as_tts_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {
                "WINDOWS_SINGLE_MACHINE": "1",
                "AI_LIVE_STUDIO_DATA": str(root),
            }, clear=False), mock.patch.object(text_studio, "_tts_runtime_status", return_value={
                "state": "stop_failed", "gpu_lease_held": True, "last_error": "still alive",
            }):
                server = text_studio.StudioServer(("127.0.0.1", 0), text_studio.make_handler(root, "http://gateway"))
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    from urllib.request import urlopen
                    with urlopen(f"http://127.0.0.1:{server.server_port}/api/runtime/status") as response:
                        body = json.load(response)
                    self.assertEqual(body["state"], "ERROR")
                    self.assertEqual(body["gpu_owner"], "tts")
                    self.assertFalse(body["model_released"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(1)


class ProviderTests(unittest.TestCase):
    def test_ollama_payload_and_json_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            with mock.patch.object(providers, "request") as request_module:
                request_module.urlopen.return_value = response
                with mock.patch.object(providers.json, "load", return_value={"message": {"content": '{"paragraphs": []}'}}):
                    content, model = providers.run_http_provider("ollama", "prompt", "qwen3:8b", root)
                payload = json.loads(request_module.Request.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(model, "qwen3:8b")
        self.assertEqual(json.loads(content), {"paragraphs": []})
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertFalse(payload["think"])

    def test_ollama_release_requires_model_to_disappear_from_ps(self):
        for running, expected in (([{"name": "qwen3:8b"}], False), ([], True)):
            generate_response = mock.MagicMock()
            generate_response.__enter__.return_value = generate_response
            generate_response.__exit__.return_value = False
            ps_response = mock.MagicMock()
            ps_response.__enter__.return_value = ps_response
            ps_response.__exit__.return_value = False
            with mock.patch.object(providers.request, "urlopen", side_effect=[generate_response, ps_response]) as urlopen:
                with mock.patch.object(providers.json, "load", side_effect=[{}, {"models": running}]):
                    self.assertEqual(providers.unload_ollama_url("http://ollama", "qwen3:8b"), expected)
            payload = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
            self.assertEqual(payload["keep_alive"], 0)


class TtsGatewayTests(unittest.TestCase):
    def test_windows_speak_path_never_calls_afplay(self):
        with tempfile.TemporaryDirectory() as directory:
            job = tts_gateway.Job("hello", "default", "test", Path(directory))
            with mock.patch.object(tts_gateway.os, "name", "nt"), mock.patch.object(tts_gateway.subprocess, "run") as run:
                tts_gateway.synthesize_and_play(job, lambda _text, _voice: b"RIFF")
            self.assertIsNone(job.error)
            run.assert_not_called()

    def test_runtime_stop_endpoint_reports_engine_stop_result(self):
        class Engine:
            def __init__(self):
                self.called = False

            def stop_now(self):
                self.called = True
                return True

            def state(self):
                return "sleeping"

            def stats(self):
                return {"state": "sleeping", "gpu_lease_held": False}

        engine = Engine()
        server = tts_gateway.Gateway(("127.0.0.1", 0), tts_gateway.make_handler(
            tts_gateway.Queue(), mock.Mock(), tts_gateway.RateLimiter(30), "key", engine,
            lambda _text, _voice: b"RIFF", 200,
        ))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            from urllib.request import Request, urlopen
            request = Request(
                f"http://127.0.0.1:{server.server_port}/runtime/stop",
                data=b"{}",
                headers={"Authorization": "Bearer key", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                body = json.load(response)
            self.assertTrue(engine.called)
            self.assertTrue(body["stopped"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_settings_endpoint_never_returns_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = text_studio.StudioServer(("127.0.0.1", 0), text_studio.make_handler(root, "http://127.0.0.1:1"))
            import threading
            from urllib.request import Request, urlopen
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with urlopen(Request(url + "/api/settings/models", method="GET")) as response:
                    value = json.load(response)
                self.assertNotIn('"api_key":', json.dumps(value))
            finally:
                server.shutdown(); server.server_close(); thread.join()


class WindowsAsrTests(unittest.TestCase):
    def test_asr_gpu_owner_stays_until_worker_task_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "gpu-owner.json"
            upload_dir = root / "upload"
            upload_dir.mkdir()
            upload = upload_dir / "input.wav"
            upload.write_bytes(b"RIFF")
            started = threading.Event()
            release = threading.Event()

            def transcribe(*_args, **kwargs):
                started.set()
                release.wait(2)
                return "最终文稿"

            with mock.patch.dict(os.environ, {
                "WINDOWS_SINGLE_MACHINE": "1",
                "AI_LIVE_STUDIO_GPU_LOCK": str(lock),
            }, clear=False):
                store = TranscriptJobStore(root, root / "model")
                job = TranscriptJob("a" * 32, "input.wav", 5, upload)
                with mock.patch.object(recording_transcript, "transcribe_recording", side_effect=transcribe):
                    thread = threading.Thread(target=store._run, args=(job,))
                    thread.start()
                    self.assertTrue(started.wait(1))
                    self.assertEqual(FileGpuLease.read(lock)["owner"], "asr")
                    release.set()
                    thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(job.stage, "completed")
            self.assertEqual(FileGpuLease.read(lock), {})

    def test_asr_worker_lifecycle_failure_keeps_owner_and_upload_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "gpu-owner.json"
            upload_dir = root / "upload"
            upload_dir.mkdir()
            upload = upload_dir / "input.wav"
            upload.write_bytes(b"RIFF")
            worker_error = qwen_asr_windows.WorkerLifecycleError("worker still alive")
            wrapped = recording_transcript.TranscriptError("worker still alive")
            wrapped.__cause__ = worker_error
            with mock.patch.dict(os.environ, {
                "WINDOWS_SINGLE_MACHINE": "1",
                "AI_LIVE_STUDIO_GPU_LOCK": str(lock),
            }, clear=False):
                store = TranscriptJobStore(root, root / "model")
                job = TranscriptJob("b" * 32, "input.wav", 5, upload)
                with mock.patch.object(recording_transcript, "transcribe_recording", side_effect=wrapped):
                    thread = threading.Thread(target=store._run, args=(job,))
                    thread.start()
                    thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(job.stage, "failed")
            self.assertEqual(FileGpuLease.read(lock)["owner"], "asr")
            self.assertTrue(upload.exists())

    def test_split_wav_respects_hard_maximum_and_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "long.wav"
            rate = 16000
            # Alternating tone/silence gives the splitter useful boundaries.
            frames = (b"\x01\x00" * (rate * 30)) + (b"\x00\x00" * (rate * 2)) + (b"\x01\x00" * (rate * 35))
            with wave.open(str(source), "wb") as handle:
                handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(rate); handle.writeframes(frames)
            chunks = split_wav(source, root / "chunks")
            self.assertTrue(chunks)
            self.assertEqual(chunks[0].start, 0.0)
            self.assertAlmostEqual(chunks[-1].end, 67.0, places=1)
            self.assertTrue(all(chunk.end - chunk.start <= 60.0 for chunk in chunks))

    def test_worker_dispatch_does_not_run_cuda_in_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, model = root / "audio.wav", root / "model"
            process = mock.Mock(returncode=0)
            process.communicate.return_value = ('{"text":"文本","segments":[]}', "")
            process.poll.return_value = 0
            with mock.patch.object(qwen_asr_windows.subprocess, "Popen", return_value=process) as popen:
                with mock.patch.object(qwen_asr_windows, "validate_model", return_value=model):
                    result = qwen_asr_windows.transcribe(audio, model)
            self.assertEqual(result["text"], "文本")
            self.assertEqual(popen.call_args.args[0][:3], [qwen_asr_windows.sys.executable, "-m", "recording_transcript.windows_worker"])
            process.communicate.assert_called_once_with(timeout=3600)
            self.assertEqual(process.poll.call_count, 1)

    def test_worker_that_remains_alive_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, model = root / "audio.wav", root / "model"
            process = mock.Mock(returncode=0)
            process.communicate.return_value = ('{"text":"文本","segments":[]}', "")
            process.poll.side_effect = [None, None]
            with mock.patch.object(qwen_asr_windows.subprocess, "Popen", return_value=process):
                with mock.patch.object(qwen_asr_windows, "validate_model", return_value=model):
                    with self.assertRaises(qwen_asr_windows.WorkerLifecycleError):
                        qwen_asr_windows.transcribe(audio, model)
            process.kill.assert_called_once_with()
            self.assertEqual(process.communicate.call_count, 2)


class StrictPlaybackTests(unittest.TestCase):
    class Player:
        def __init__(self, fail=False):
            self.ids = []
            self.fail = fail

        def play(self, path, stop_event, pause_event):
            self.ids.append(path.stem)
            if self.fail and len(self.ids) == 1:
                raise RuntimeError("device failed")
            stop_event.set()

    def test_strict_session_waits_for_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _item(root, "later", 2, "s1")
            player = self.Player()
            controller = PlaybackController(root, player=player, strict_session=True, session_id="s1")
            controller.start()
            time.sleep(0.25)
            self.assertEqual(player.ids, [])
            _item(root, "first", 1, "s1")
            deadline = time.time() + 1
            while not player.ids and time.time() < deadline:
                time.sleep(0.01)
            controller.stop()
            self.assertEqual(player.ids, ["first"])

    def test_strict_session_does_not_skip_failed_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _item(root, "first", 1, "s1")
            _item(root, "second", 2, "s1")
            player = self.Player(fail=True)
            controller = PlaybackController(root, player=player, strict_session=True, session_id="s1")
            controller.start()
            deadline = time.time() + 1
            while controller.is_running() and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(player.ids, ["first"])
            self.assertEqual(controller.stats()["playback_failed"], 1)


if __name__ == "__main__":
    unittest.main()
