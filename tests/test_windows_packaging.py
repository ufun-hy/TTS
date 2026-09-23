"""Checks for the user-facing Windows package and startup preflight."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_launcher_runtime", ROOT / "scripts/windows-runtime.py")
launcher = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(launcher)


class WindowsPackagingTests(unittest.TestCase):
    def test_models_directory_can_be_changed_without_touching_models(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            selected = data / "another-drive" / "AI-Live-Studio-Models"
            selected.mkdir(parents=True)
            (data / "config").mkdir()
            (data / "config" / "models-path.json").write_text(
                json.dumps({"models_dir": str(selected)}), encoding="utf-8"
            )
            self.assertEqual(launcher._configured_models(data), selected)

    def test_startup_preflight_explains_missing_external_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = launcher.startup_errors(
                root / "models",
                root / "data",
                root / "runtime" / "python" / "python.exe",
                root / "runtime" / "bin",
                check_hardware=False,
            )
            detail = "\n".join(errors)
            self.assertIn("Qwen3-ASR-1.7B 未找到", detail)
            self.assertIn("CosyVoice3 Q8 模型未找到", detail)
            self.assertIn("Qwen3 8B / Ollama 模型未找到或不完整", detail)
            self.assertIn("固定 Python Runtime 未随安装包提供", detail)
            self.assertIn("ffprobe 未随安装包提供", detail)

    def test_empty_ollama_store_subdirectories_are_not_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            (models / "llm" / "ollama-store" / "blobs").mkdir(parents=True)
            (models / "llm" / "ollama-store" / "manifests").mkdir(parents=True)
            (models / "llm" / "Modelfile").write_text("FROM ./Qwen3-8B-Q4_K_M.gguf\n", encoding="utf-8")
            errors = launcher._model_errors(models, root / "bin")
            self.assertIn("Qwen3 8B / Ollama 模型未找到或不完整", "\n".join(errors))

    def test_product_files_use_new_installer_name(self):
        workflow = (ROOT / ".github/workflows/windows-client-build.yml").read_text(encoding="utf-8")
        installer = (ROOT / "build/windows/AI-Live-Studio.iss").read_text(encoding="utf-8")
        build = (ROOT / "build/windows/build.ps1").read_text(encoding="utf-8")
        self.assertIn("AI-Live-Studio-Windows-Test-Setup.exe", workflow)
        self.assertIn("AI-Live-Studio-Windows-Test-Setup", installer)
        self.assertIn("AI-Live-Studio-Windows-Test-Setup.exe", build)
        self.assertNotIn("AI-Audio-Client-Setup.exe", workflow)

    def test_tts_and_online_provider_secrets_have_separate_dpapi_files(self):
        from local_runtime.settings import secret_store, tts_secret_store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertNotEqual(secret_store(root).path, tts_secret_store(root).path)
            self.assertTrue(str(tts_secret_store(root).path).endswith("tts-api-key.dpapi"))

    def test_runtime_components_keep_backend_dlls_out_of_python_path(self):
        entries = launcher.commands(Path("models"), Path("data"), Path("python"), Path("runtime/bin"))
        expected_engine = Path("runtime/bin/cosyvoice") / ("cosyvoice-server.exe" if os.name == "nt" else "cosyvoice-server")
        self.assertIn(str(expected_engine), entries["tts-gateway"][0])
        self.assertEqual(entries["ollama"][1], Path("runtime/bin/ollama"))

    def test_windows_tts_selects_vulkan_gpu_device(self):
        paths = Path("models"), Path("data"), Path("python"), Path("runtime/bin")
        with mock.patch.object(launcher.os, "name", "nt"):
            command = launcher.commands(*paths)["tts-gateway"][0]
        self.assertEqual(command[command.index("--engine-backend") + 1], "Vulkan0")

    def test_single_machine_audio_client_uses_headless_playback_service(self):
        entries = launcher.commands(Path("models"), Path("data"), Path("python"), Path("runtime/bin"))
        self.assertEqual(entries["audio-client"][0][1], str(ROOT / "windows_playback_service.py"))
        self.assertNotIn("windows_client.py", entries["audio-client"][0])

    def test_headless_playback_service_imports_without_tkinter(self):
        code = '''
import importlib.abc
import sys

class BlockTk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tkinter" or fullname.startswith("tkinter."):
            raise ModuleNotFoundError("tkinter blocked")

sys.meta_path.insert(0, BlockTk())
sys.path.insert(0, sys.argv[1])
import windows_playback_service
'''
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        result = subprocess.run(
            [sys.executable, '-S', '-c', code, str(ROOT)],
            cwd=tempfile.gettempdir(), env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_llm_uses_bundled_ollama_component_path(self):
        args = type("Args", (), {
            "models": "D:/AI-Live-Studio-Models", "bin_dir": "runtime/bin", "dry_run": True,
        })()
        with mock.patch("builtins.print") as output:
            self.assertEqual(launcher.import_llm(args), 0)
        command = json.loads(output.call_args.args[0])
        self.assertEqual(command[0], str(Path("runtime/bin/ollama") / ("ollama.exe" if os.name == "nt" else "ollama")))


if __name__ == "__main__":
    unittest.main()
