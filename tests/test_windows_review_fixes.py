"""Regression coverage for the pre-hardware Windows review blockers."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from local_runtime import FileGpuLease, RuntimeManager, RuntimeStage, RuntimeBusyError
from local_runtime import file_lease, update
from server import text_studio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_launcher", ROOT / "scripts/windows-runtime.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ProcessProbeTests(unittest.TestCase):
    def test_windows_probe_never_signals_a_process(self):
        with mock.patch.object(file_lease, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(file_lease, "_windows_pid_alive", return_value=True) as probe:
            self.assertTrue(file_lease._pid_alive(123))
        probe.assert_called_once_with(123)

    def test_windows_handle_probe_alive_exited_and_unknown(self):
        for wait_result, expected in ((258, True), (0, False), (0xFFFFFFFF, True)):
            with self.subTest(wait_result=wait_result):
                kernel = mock.Mock()
                kernel.OpenProcess.return_value = 100
                kernel.WaitForSingleObject.return_value = wait_result
                with mock.patch.object(file_lease.ctypes, "WinDLL", return_value=kernel, create=True):
                    self.assertEqual(file_lease._windows_pid_alive(123), expected)
                kernel.OpenProcess.assert_called_once_with(0x00100000, False, 123)
                kernel.WaitForSingleObject.assert_called_once_with(100, 0)
                kernel.CloseHandle.assert_called_once_with(100)

    def test_windows_access_denied_does_not_mean_dead(self):
        for error, expected in ((5, True), (87, False)):
            kernel = mock.Mock()
            kernel.OpenProcess.return_value = None
            with mock.patch.object(file_lease.ctypes, "WinDLL", return_value=kernel, create=True), \
                    mock.patch.object(file_lease.ctypes, "get_last_error", return_value=error, create=True):
                self.assertEqual(file_lease._windows_pid_alive(123), expected)
            kernel.CloseHandle.assert_not_called()

    def test_other_process_keeps_its_lease_and_stays_alive(self):
        root = Path(tempfile.mkdtemp(prefix="tts-cross-process-review-"))
        path = root / "gpu.json"
        code = """
import sys
from local_runtime.file_lease import FileGpuLease
lease = FileGpuLease(sys.argv[1], 'child', 'model', 'ASR')
assert lease.acquire()
print('ready', flush=True)
try:
    if sys.stdin.readline().strip() == 'ping':
        print('alive', flush=True)
finally:
    lease.release()
"""
        child = subprocess.Popen([sys.executable, "-u", "-c", code, str(path)],
                                 cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertNotEqual(child.pid, os.getpid())
            self.assertFalse(FileGpuLease(path, "parent").acquire())
            self.assertEqual(FileGpuLease.read(path)["pid"], child.pid)
            self.assertIsNone(child.poll())
            out, err = child.communicate("ping\n", timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertEqual(out.strip(), "alive")
            self.assertFalse(file_lease._pid_alive(child.pid))
            parent = FileGpuLease(path, "parent")
            self.assertTrue(parent.acquire())
            self.assertTrue(parent.release())
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


class StopTests(unittest.TestCase):
    @staticmethod
    def _record():
        return launcher._record(
            "text-studio", 123,
            ["C:/AI-Live-Studio/runtime/python/python.exe", "C:/AI-Live-Studio/server/text_studio_entry.py"],
        )

    @staticmethod
    def _info():
        return {
            "pid": "123",
            "executable": "C:/AI-Live-Studio/runtime/python/python.exe",
            "command_line": "C:/AI-Live-Studio/runtime/python/python.exe C:/AI-Live-Studio/server/text_studio_entry.py",
        }

    def test_stop_preserves_processes_even_when_taskkill_reports_success(self):
        for returncode in (0, 1):
            record = self._record()
            with self.subTest(returncode=returncode), \
                    mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), \
                    mock.patch.object(launcher, "_running", return_value=True), \
                    mock.patch.object(launcher, "_process_info", return_value=self._info()), \
                    mock.patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(
                        returncode=returncode, stdout="", stderr="denied")):
                self.assertEqual(launcher._stop_processes({"text-studio": record}, timeout=0), {"text-studio": record})

    def test_stop_forgets_only_confirmed_exit(self):
        with mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(launcher, "_running", side_effect=[True, False, False]), \
                mock.patch.object(launcher, "_process_info", return_value=self._info()), \
                mock.patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(
                        returncode=1, stdout="", stderr="process exited concurrently")):
            self.assertEqual(launcher._stop_processes({"text-studio": self._record()}, timeout=0), {})

    def test_stop_command_persists_survivors_and_returns_nonzero(self):
        records = {"text-studio": self._record(), "audio-cache": {"pid": 456}}
        survivor = {"text-studio": self._record()}
        with mock.patch.object(launcher, "_load_processes", return_value=records), \
                mock.patch.object(launcher, "_stop_processes", return_value=survivor), \
                mock.patch.object(launcher, "_untracked_listeners", return_value=[]), \
                mock.patch.object(launcher, "_write_processes") as write:
            self.assertEqual(launcher.stop(SimpleNamespace(data="synthetic-data")), 1)
            self.assertEqual(write.call_args.args[1], survivor)

    def test_taskkill_timeout_keeps_pid(self):
        record = self._record()
        with mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(launcher, "_running", return_value=True), \
                mock.patch.object(launcher, "_process_info", return_value=self._info()), \
                mock.patch.object(launcher.subprocess, "run", side_effect=subprocess.TimeoutExpired("taskkill", 0)):
            self.assertEqual(launcher._stop_processes({"text-studio": record}, timeout=0), {"text-studio": record})


class UpdateSourceTests(unittest.TestCase):
    def test_insecure_manifest_is_rejected_before_network_access(self):
        for url in ("http://example.invalid/update.json", "file:///tmp/update.json", "http://127.0.0.1/update.json"):
            manager = update.UpdateManager(Path("synthetic-data"), manifest_url=url)
            with mock.patch.object(update.request, "build_opener") as opener:
                status = manager.check()
            self.assertIn("HTTPS", status["error"])
            self.assertIsNone(manager.manifest)
            opener.assert_not_called()

    def test_https_manifest_is_accepted(self):
        manifest = {"version": "1.0.1", "installer_url": "https://example.invalid/app.exe", "sha256": "a" * 64}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(manifest).encode()
        with mock.patch.object(update.request, "build_opener") as build:
            build.return_value.open.return_value = response
            manager = update.UpdateManager(Path("synthetic-data"), manifest_url="https://example.invalid/update.json")
            self.assertTrue(manager.check()["update_available"])

    def test_redirect_cannot_downgrade_to_http(self):
        handler = update._HTTPSRedirectHandler()
        with self.assertRaises(update.UpdateError):
            handler.redirect_request(Request("https://example.invalid/update.json"), None,
                                     302, "Found", {}, "http://example.invalid/update.json")

    def test_failed_check_invalidates_cached_installer_reference(self):
        manager = update.UpdateManager(Path("synthetic-data"), manifest_url="http://example.invalid/update.json")
        manager.downloaded = Path("old-installer.exe")
        manager.check()
        self.assertIsNone(manager.downloaded)


class RecoveryTests(unittest.TestCase):
    def test_recovery_verifies_release_and_preserves_owner_until_confirmed(self):
        confirm = mock.Mock(return_value=False)
        runtime = RuntimeManager()
        lease = runtime.acquire(RuntimeStage.REWRITE, "ollama", "qwen3:8b", confirm_release=confirm)
        runtime.release(lease, "unload failed")
        self.assertTrue(runtime.snapshot()["recovery_available"])
        self.assertFalse(runtime.retry_release(lease.token))
        with self.assertRaises(RuntimeBusyError):
            runtime.acquire(RuntimeStage.LIVE, "tts")
        confirm.return_value = True
        self.assertTrue(runtime.retry_release(lease.token))
        self.assertEqual(runtime.snapshot()["state"], "IDLE")
        self.assertEqual(runtime.snapshot()["last_error"], "")

    def test_recovery_rejects_active_stale_or_unverifiable_operation(self):
        runtime = RuntimeManager()
        check = mock.Mock(return_value=True)
        lease = runtime.acquire(RuntimeStage.ASR, "asr", confirm_release=check)
        with self.assertRaises(RuntimeError):
            runtime.retry_release(lease.token)
        runtime.release(lease, "failed")
        with self.assertRaises(RuntimeError):
            runtime.retry_release("stale-token")
        check.assert_not_called()
        unverified = RuntimeManager()
        unknown = unverified.acquire(RuntimeStage.ASR, "asr")
        unverified.release(unknown, "unknown worker")
        with self.assertRaises(RuntimeError):
            unverified.retry_release(unknown.token)

    def test_recovery_verifier_exception_keeps_error(self):
        runtime = RuntimeManager()
        lease = runtime.acquire(RuntimeStage.REWRITE, "ollama", confirm_release=mock.Mock(side_effect=OSError("offline")))
        runtime.release(lease, "failed")
        self.assertFalse(runtime.retry_release(lease.token))
        self.assertEqual(runtime.snapshot()["gpu_owner"], "ollama")
        self.assertIn("offline", runtime.snapshot()["last_error"])

    def test_preview_and_ollama_can_recover_after_http_handler_returns(self):
        for operation in ("preview", "ollama"):
            with self.subTest(operation=operation):
                root = Path(tempfile.mkdtemp(prefix="tts-http-recovery-review-"))
                confirm = mock.Mock(side_effect=[False, False, True])
                with mock.patch.dict(os.environ, {"WINDOWS_SINGLE_MACHINE": "1", "AI_LIVE_STUDIO_DATA": str(root)}), \
                        mock.patch.object(text_studio, "_tts_runtime_status", return_value={}), \
                        mock.patch.object(text_studio, "_stop_tts_engine", confirm if operation == "preview" else mock.Mock(return_value=True)), \
                        mock.patch.object(text_studio, "unload_ollama_url", confirm if operation == "ollama" else mock.Mock(return_value=True)), \
                        mock.patch.object(text_studio, "provider_config", return_value={"model": "qwen3:8b", "base_url": "http://original-ollama"}), \
                        mock.patch.object(text_studio, "_tts_preview", return_value={"success": True}), \
                        mock.patch.object(text_studio, "generalize_paragraphs", return_value=[]):
                    server = text_studio.StudioServer(("127.0.0.1", 0), text_studio.make_handler(root, "http://gateway", tts_api_key="synthetic"))
                    thread = threading.Thread(target=server.serve_forever)
                    thread.start()
                    def call(path, body=None, origin=None):
                        headers = {"Content-Type": "application/json"}
                        if origin is not None:
                            headers["Origin"] = origin
                        req = Request(f"http://127.0.0.1:{server.server_port}{path}",
                                      data=json.dumps(body).encode() if body is not None else None,
                                      headers=headers)
                        try:
                            response = urlopen(req, timeout=3)
                        except HTTPError as exc:
                            response = exc
                        with response:
                            return response.status, json.load(response)
                    try:
                        path = "/api/tts/preview" if operation == "preview" else "/api/generalize"
                        payload = {"text": "test", "voice": "default"} if operation == "preview" else {"provider": "ollama", "paragraphs": [], "model": "qwen3:8b"}
                        self.assertEqual(call(path, payload)[0], 200)
                        status = call("/api/runtime/status")[1]
                        self.assertEqual(status["state"], "ERROR")
                        token = status["operation"]
                        for origin in ("https://example.invalid", "null"):
                            self.assertEqual(call("/api/runtime/recover", {"operation": token}, origin)[0], 403)
                        self.assertEqual(call("/api/runtime/recover", {"operation": "stale"})[0], 409)
                        self.assertEqual(call("/api/runtime/recover", {"operation": token})[0], 409)
                        code, result = call("/api/runtime/recover", {"operation": token})
                        self.assertEqual(code, 200)
                        self.assertTrue(result["recovered"])
                        self.assertEqual(result["runtime"]["state"], "IDLE")
                        self.assertFalse((root / "runtime/gpu-owner.json").exists())
                        if operation == "ollama":
                            self.assertEqual(confirm.call_args.args, ("http://original-ollama", "qwen3:8b"))
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(2)


if __name__ == "__main__":
    unittest.main()
