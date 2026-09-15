import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

from audio_client.playback import PlaybackController
from local_runtime import FileGpuLease, RuntimeBusyError, RuntimeManager, RuntimeStage, UpdateError, validate_manifest
from server import text_studio_http_providers as providers
from server.live_session_blocks import SynthesisBlockLiveSessionManager
from server import text_studio
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
            with mock.patch.object(qwen_asr_windows.subprocess, "run", return_value=mock.Mock(returncode=0, stdout='{"text":"文本","segments":[]}', stderr="")) as run:
                with mock.patch.object(qwen_asr_windows, "validate_model", return_value=model):
                    result = qwen_asr_windows.transcribe(audio, model)
            self.assertEqual(result["text"], "文本")
            self.assertEqual(run.call_args.args[0][:3], [qwen_asr_windows.sys.executable, "-m", "recording_transcript.windows_worker"])


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
