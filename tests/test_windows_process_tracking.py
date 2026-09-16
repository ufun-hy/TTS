"""Windows Runtime process ownership, listener and health diagnostics."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import urlopen

from server import text_studio
import windows_launcher


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_runtime_tracking", ROOT / "scripts/windows-runtime.py")
launcher = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(launcher)


def _record(name="text-studio", pid=10):
    return launcher._record(name, pid, [
        r"C:\AI-Live-Studio\runtime\python\python.exe",
        r"C:\AI-Live-Studio\server\text_studio_entry.py",
    ])


def _info(pid=10, command=None):
    command = command or r"C:\AI-Live-Studio\runtime\python\python.exe C:\AI-Live-Studio\server\text_studio_entry.py"
    return {
        "pid": str(pid),
        "executable": r"C:\AI-Live-Studio\runtime\python\python.exe",
        "command_line": command,
    }


class ProcessTrackingTests(unittest.TestCase):
    def test_process_file_round_trip_keeps_identity_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "windows-processes.json"
            record = _record()
            launcher._write_processes(path, {"text-studio": record})
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 2)
            self.assertEqual(raw["services"]["text-studio"]["pid"], 10)
            self.assertEqual(launcher._load_processes(path)["text-studio"], record)

    def test_legacy_pid_file_is_read_with_service_identity_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "windows-processes.json"
            path.write_text(json.dumps({"text-studio": 10}), encoding="utf-8")
            record = launcher._load_processes(path)["text-studio"]
            self.assertEqual(record["pid"], 10)
            self.assertEqual(record["expected_entrypoint"], "server/text_studio_entry.py")

    def test_launcher_pid_stale_but_child_listener_is_reported(self):
        record = _record(pid=10)
        with mock.patch.object(launcher, "_running", return_value=False), \
                mock.patch.object(launcher, "_listener_pids", return_value=[20]), \
                mock.patch.object(launcher, "_process_info", return_value=_info(20)):
            state = launcher._inspect_process("text-studio", record)
            self.assertFalse(state["running"])
            self.assertIn("stale_pid", state["issues"])
            self.assertIn("untracked_listener", state["issues"])
            self.assertEqual(state["listener_pid"], 20)

    def test_stale_pid_without_listener_is_not_running(self):
        with mock.patch.object(launcher, "_running", return_value=False), \
                mock.patch.object(launcher, "_listener_pids", return_value=[]):
            state = launcher._inspect_process("text-studio", _record())
            self.assertFalse(state["running"])
            self.assertEqual(state["issues"], ["stale_pid"])

    def test_pid_reuse_fails_identity_validation(self):
        foreign = _info(command=r"C:\Windows\System32\python.exe C:\other\server.py")
        with mock.patch.object(launcher, "_running", return_value=True), \
                mock.patch.object(launcher, "_process_info", return_value=foreign), \
                mock.patch.object(launcher, "_listener_pids", return_value=[]):
            state = launcher._inspect_process("text-studio", _record())
            self.assertTrue(state["running"])
            self.assertFalse(state["owned"])
            self.assertIn("ownership_mismatch", state["issues"])

    def test_listener_pid_can_be_resolved_after_launcher_pid_exits(self):
        record = _record(pid=10)
        with mock.patch.object(launcher, "_listener_pids", return_value=[20]), \
                mock.patch.object(launcher, "_process_info", return_value=_info(20)):
            self.assertEqual(launcher._wait_for_owned_listener("text-studio", record, timeout=0.1), 20)

    def test_all_listener_checks_share_one_parallel_deadline(self):
        records = {f"service-{index}": {"pid": index + 1, "port": index + 2000} for index in range(5)}
        barrier = threading.Barrier(len(records))

        def wait_for_listener(*_args):
            barrier.wait(timeout=1)
            return None

        with mock.patch.object(launcher, "_wait_for_owned_listener", side_effect=wait_for_listener):
            resolved = launcher._resolve_owned_listeners(records, timeout=1)
        self.assertEqual(set(resolved), set(records))
        self.assertTrue(all(value is None for value in resolved.values()))

    def test_stop_only_kills_owned_process(self):
        owned = _record(pid=10)
        foreign = _record(pid=11)
        foreign["expected_entrypoint"] = "server/other.py"
        fake_os = mock.Mock()
        fake_os.name = "nt"
        with mock.patch.object(launcher, "os", fake_os), \
                mock.patch.object(launcher, "_running", side_effect=[True, False, False, True]), \
                mock.patch.object(launcher, "_process_info", side_effect=[_info(10), _info(11, r"C:\other\server.py")]), \
                mock.patch.object(launcher.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            remaining = launcher._stop_processes({"text-studio": owned, "foreign": foreign}, timeout=0)
        self.assertIn("foreign", remaining)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "taskkill")

    def test_untracked_listener_is_reported_without_kill(self):
        record = _record(pid=10)
        ports = {port: [] for port in launcher.SERVICE_PORTS.values()}
        ports[8766] = []
        ports[8770] = [20]
        with mock.patch.object(launcher, "_listener_pids", side_effect=lambda port: ports[port]), \
                mock.patch.object(launcher, "_process_info", return_value=_info(20, r"C:\other\server.py")):
            listeners = launcher._untracked_listeners({"text-studio": record})
        self.assertEqual([(item["port"], item["pid"]) for item in listeners], [(8770, 20)])

    def test_cosyvoice_engine_child_of_gateway_is_not_external(self):
        gateway = _record("tts-gateway", pid=30)
        ports = {port: [] for port in launcher.SERVICE_PORTS.values()}
        ports[8766] = [31]
        engine_info = {
            "pid": "31", "parent_pid": "30",
            "executable": r"C:\AI-Live-Studio\runtime\bin\cosyvoice-server.exe",
            "command_line": r"C:\AI-Live-Studio\runtime\bin\cosyvoice-server.exe --port 8766",
        }
        with mock.patch.object(launcher, "_listener_pids", side_effect=lambda port: ports[port]), \
                mock.patch.object(launcher, "_process_info", return_value=engine_info):
            self.assertEqual(launcher._untracked_listeners({"tts-gateway": gateway}), [])


class HealthProbeTests(unittest.TestCase):
    def test_health_probe_bypasses_proxy_and_reports_http_failure(self):
        response = mock.MagicMock(status=503)
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(launcher.request, "build_opener", return_value=opener) as build:
            detail = launcher._health_probe("text-studio", "http://127.0.0.1:8770/api/health")
        self.assertFalse(detail["ok"])
        self.assertEqual(detail["status"], 503)
        build.assert_called_once()
        self.assertIsInstance(build.call_args.args[0], launcher.request.ProxyHandler)

    def test_health_probe_reports_timeout_exception(self):
        opener = mock.Mock()
        opener.open.side_effect = TimeoutError("probe timed out")
        with mock.patch.object(launcher.request, "build_opener", return_value=opener):
            detail = launcher._health_probe("tts-gateway", "http://127.0.0.1:8765/health", timeout=0.25)
        self.assertFalse(detail["ok"])
        self.assertEqual(detail["exception_class"], "TimeoutError")
        self.assertEqual(detail["timeout_seconds"], 0.25)
        self.assertIn("timed out", detail["error"])


class TextStudioLivenessTests(unittest.TestCase):
    def test_health_is_liveness_only_and_dependencies_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = text_studio.StudioServer(("127.0.0.1", 0), text_studio.make_handler(root, "http://gateway"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with mock.patch.object(text_studio, "provider_available", side_effect=AssertionError("dependency probe")), \
                        mock.patch.object(text_studio, "_tts_health", side_effect=AssertionError("dependency probe")):
                    with urlopen(base + "/api/health") as response:
                        liveness = json.load(response)
                self.assertEqual(liveness, {"status": "ok", "service": "text-studio"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(1)


class LauncherOwnershipTests(unittest.TestCase):
    def test_launcher_does_not_reuse_healthy_unowned_text_studio(self):
        status = {
            "health": {"studio": True},
            "processes": {"text-studio": {"owned": False, "running": False}},
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(status), "")
        with mock.patch.object(windows_launcher, "_run_runtime", return_value=completed), \
                mock.patch.object(windows_launcher, "_check_and_maybe_choose", return_value=False) as check, \
                mock.patch.object(windows_launcher.webbrowser, "open") as open_browser:
            self.assertEqual(windows_launcher._start(Path("data"), mock.Mock()), 1)
        check.assert_called_once()
        open_browser.assert_not_called()


class StartupPersistenceTests(unittest.TestCase):
    def test_records_survive_launcher_exit_before_final_listener_validation(self):
        class Process:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            models = Path(directory) / "models"
            python = Path(directory) / "python.exe"
            bin_dir = Path(directory) / "bin"
            processes = [Process(index) for index in range(100, 106)]
            fake_os = mock.Mock(wraps=os)
            fake_os.name = "nt"
            fake_os.pathsep = os.pathsep
            fake_os.environ = os.environ
            with mock.patch.object(launcher, "os", fake_os), \
                    mock.patch.object(launcher, "startup_errors", return_value=[]), \
                    mock.patch.object(launcher.subprocess, "Popen", side_effect=processes), \
                    mock.patch.object(launcher, "_resolve_owned_listeners", side_effect=KeyboardInterrupt), \
                    mock.patch("local_runtime.settings.tts_secret_store") as secret_store:
                secret_store.return_value.get.return_value = "test-key"
                args = type("Args", (), {
                    "data": str(data), "models": str(models), "python": str(python),
                    "bin_dir": str(bin_dir), "dry_run": False,
                })()
                with self.assertRaises(KeyboardInterrupt):
                    launcher.start(args)
            process_file = data / "runtime" / "windows-processes.json"
            saved = json.loads(process_file.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["services"]), set(launcher.SERVICE_MATCHERS))
            self.assertEqual(saved["services"]["text-studio"]["pid"], 104)


if __name__ == "__main__":
    unittest.main()
