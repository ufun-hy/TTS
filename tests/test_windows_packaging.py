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
desktop_spec = importlib.util.spec_from_file_location("windows_desktop_launcher", ROOT / "windows_launcher.py")
desktop_launcher = importlib.util.module_from_spec(desktop_spec)
assert desktop_spec.loader is not None
desktop_spec.loader.exec_module(desktop_launcher)


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

    def test_runtime_uses_saved_model_directory_when_no_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            selected = data / "copied-models"
            (data / "config").mkdir()
            (data / "config" / "models-path.json").write_text(
                json.dumps({"models_dir": str(selected)}), encoding="utf-8")
            self.assertEqual(launcher._configured_models(data), selected)
            self.assertEqual(launcher._configured_models(data, "E:/explicit-models"), Path("E:/explicit-models"))

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

    def test_base_runtime_preflight_does_not_require_models_or_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = launcher.startup_errors(
                root / "models", root / "data", root / "runtime/python/python.exe",
                root / "runtime/bin", check_hardware=False, check_models=False,
            )
            detail = "\n".join(errors)
            self.assertNotIn("Qwen3-ASR-1.7B 未找到", detail)
            self.assertNotIn("CosyVoice3 Q8 模型未找到", detail)
            self.assertIn("固定 Python Runtime 未找到", detail)

    def test_launcher_checks_base_runtime_without_model_or_gpu_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(desktop_launcher, "_run_runtime", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps({"ok": True, "model_status": {"tts": {"ready": False}}}), "")) as run:
                self.assertTrue(desktop_launcher._check_and_maybe_choose(Path(directory), None))
            self.assertIn("--allow-missing-models", run.call_args.kwargs["extra_args"])
            self.assertIn("--skip-hardware", run.call_args.kwargs["extra_args"])

    def test_launcher_waits_for_base_workspace_only(self):
        status = {"health": {"studio": True, "tts": False, "asr": False},
                  "processes": {"text-studio": {"owned": True, "running": True}}}
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(desktop_launcher, "_run_runtime", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(status), "")):
                self.assertTrue(desktop_launcher._wait_for_runtime(Path(directory), None))

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
        bootstrap = (ROOT / "build/windows/AI-Live-Studio-Bootstrap.spec").read_text(encoding="utf-8")
        self.assertIn("AI-Live-Studio-Windows-Test-Setup.exe", workflow)
        self.assertIn("AI-Live-Studio-Windows-Test-Setup", installer)
        self.assertIn("AI-Live-Studio-Windows-Test-Setup.exe", build)
        self.assertIn("AI-Live-Studio-Bootstrap.spec", build)
        self.assertIn("update-source.json", bootstrap)
        self.assertNotIn("model-source.json", bootstrap)
        self.assertIn("Check Local Models", installer)
        self.assertNotIn("AI-Audio-Client-Setup.exe", workflow)

    def test_cos_program_release_does_not_require_patch_or_models(self):
        for script, expected, models_optional in (
            ("build/windows/prepare_cos_release.py", "--installer-dir", True),
            ("build/windows/sign_release.py", "--installer", False),
        ):
            result = subprocess.run(
                [sys.executable, str(ROOT / script), "--help"],
                capture_output=True, text=True, check=True, timeout=15,
            )
            self.assertIn(expected, result.stdout)
            self.assertIn("[--patch PATCH]", result.stdout)
            if models_optional:
                self.assertIn("[--models-dir MODELS_DIR]", result.stdout)

    def test_patch_archive_contains_audio_cache_control_service(self):
        workflow = (ROOT / ".github/workflows/windows-patch-build.yml").read_text(encoding="utf-8")
        self.assertIn('"audio_cache/**"', workflow)
        self.assertIn('"audio_cache"', workflow)

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
        self.assertEqual(command[command.index("--engine-backend") + 1], "nvidia-vulkan")

    def test_obsolete_numeric_backend_does_not_select_integrated_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory)
            (data/'config').mkdir()
            (data/'config/tts-backend.json').write_text('{"backend":"Vulkan1"}')
            paths = Path('models'), data, Path('python'), Path('bin')
            with mock.patch.object(launcher.os, 'name', 'nt'):
                command=launcher.commands(*paths)['tts-gateway'][0]
            self.assertEqual(command[command.index('--engine-backend')+1],'nvidia-vulkan')

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
            "models": "D:/AI-Live-Studio-Models", "data": "runtime/data",
            "bin_dir": "runtime/bin", "dry_run": True,
        })()
        with mock.patch("builtins.print") as output:
            self.assertEqual(launcher.import_llm(args), 0)
        command = json.loads(output.call_args.args[0])
        self.assertEqual(command[0], str(Path("runtime/bin/ollama") / ("ollama.exe" if os.name == "nt" else "ollama")))

    def test_import_llm_uses_manually_selected_directory_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            selected = data / "copied-models"
            (data / "config").mkdir()
            (data / "config" / "models-path.json").write_text(
                json.dumps({"models_dir": str(selected)}), encoding="utf-8")
            args = type("Args", (), {
                "models": "", "data": str(data), "bin_dir": "runtime/bin", "dry_run": True,
            })()
            with mock.patch("builtins.print") as output:
                self.assertEqual(launcher.import_llm(args), 0)
            command = json.loads(output.call_args.args[0])
            self.assertEqual(command[-1], str(selected / "llm" / "Modelfile"))


if __name__ == "__main__":
    unittest.main()
