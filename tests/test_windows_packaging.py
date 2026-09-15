"""Checks for the user-facing Windows package and startup preflight."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


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

    def test_runtime_components_keep_cuda_dlls_out_of_python_path(self):
        entries = launcher.commands(Path("models"), Path("data"), Path("python"), Path("runtime/bin"))
        expected_engine = Path("runtime/bin/cosyvoice") / ("cosyvoice-server.exe" if os.name == "nt" else "cosyvoice-server")
        self.assertIn(str(expected_engine), entries["tts-gateway"][0])
        self.assertEqual(entries["ollama"][1], Path("runtime/bin/ollama"))


if __name__ == "__main__":
    unittest.main()
