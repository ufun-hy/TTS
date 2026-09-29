"""Real filesystem transactions, fake runtime; safe on Mac and Windows CI."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from local_runtime import offline_patch as patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_app", ROOT / "build/windows/package_app.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class OfflinePatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app, self.data, self.package = root / "app", root / "data", root / "patch"
        self.runtime = mock.Mock()
        self.make_app(self.app, "1", {"server/example.py": "old", "AI-Live-Studio.exe": "old exe"})
        self.make_app(self.package, "2", {"server/example.py": "new", "AI-Live-Studio.exe": "new exe",
                                          "web/new.js": "new page"})
        for name in ("config/secrets.dpapi", "cache/audio.wav", "projects/project.json", "voices/custom.gguf"):
            path = self.data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"user data")
        (root / "models").mkdir()
        (root / "models/voice.gguf").write_bytes(b"model")

    def make_app(self, root, commit, contents):
        files = {}
        for name, text in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            files[name] = patch.digest(path)
        native = root / "runtime/bin/cosyvoice/ggml-vulkan.dll"
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_bytes(b"tested runtime")
        patch.write_json(root / "app-manifest.json", {
            "schema": 1, "commit": commit * 40, "runtime_id": "f" * 64, "files": files,
            "runtime_files": {"runtime/bin/cosyvoice/ggml-vulkan.dll": patch.digest(native)},
        })

    def apply(self):
        return patch.apply(self.package, self.app, self.data, self.runtime)

    def test_apply_verifies_and_rollback_restores_including_launcher(self):
        backup = self.apply()
        self.assertEqual((self.app / "AI-Live-Studio.exe").read_text(), "new exe")
        self.assertEqual(patch.read_json(backup / "transaction.json")["state"], "committed")
        self.runtime.start.assert_called_once()
        patch.rollback(backup, self.app, self.runtime)
        self.assertEqual((self.app / "AI-Live-Studio.exe").read_text(), "old exe")
        self.assertFalse((self.app / "web/new.js").exists())
        self.assertEqual((backup / "displaced/web/new.js").read_text(), "new page")
        self.assertEqual((self.data / "config/secrets.dpapi").read_bytes(), b"user data")
        self.assertEqual((self.app.parent / "models/voice.gguf").read_bytes(), b"model")

    def test_health_failure_automatically_restores_files_and_leaves_stopped(self):
        self.runtime.start.side_effect = patch.PatchError("TTS health timeout")
        with self.assertRaisesRegex(patch.PatchError, "old files restored"):
            self.apply()
        self.assertEqual((self.app / "server/example.py").read_text(), "old")
        self.assertFalse((self.app / "web/new.js").exists())
        self.assertEqual(self.runtime.stop.call_count, 2)

    def test_rejects_legacy_runtime_mismatch_and_modified_files_before_stop(self):
        path = self.app / "app-manifest.json"
        old = path.read_bytes()
        path.rename(self.app / "legacy-manifest.json")
        with self.assertRaisesRegex(patch.PatchError, "Legacy"):
            self.apply()
        path.write_bytes(old)
        data = patch.read_json(path)
        data["runtime_id"] = "e" * 64
        patch.write_json(path, data)
        with self.assertRaisesRegex(patch.PatchError, "dependencies differ"):
            self.apply()
        path.write_bytes(old)
        (self.app / "server/example.py").write_text("hand edit")
        with self.assertRaisesRegex(patch.PatchError, "modified"):
            self.apply()
        self.runtime.stop.assert_not_called()

    def test_corrupt_payload_or_native_dll_rejected_before_stop(self):
        path = self.package / "server/example.py"
        path.write_text("corrupt download")
        with self.assertRaisesRegex(patch.PatchError, "modified"):
            self.apply()
        path.write_text("new")
        (self.app / "runtime/bin/cosyvoice/ggml-vulkan.dll").write_bytes(b"wrong backend")
        with self.assertRaisesRegex(patch.PatchError, "modified"):
            self.apply()
        self.runtime.stop.assert_not_called()

    def test_unmanaged_file_collision_and_case_duplicate_rejected(self):
        (self.app / "web").mkdir()
        (self.app / "web/new.js").write_text("user file")
        with self.assertRaisesRegex(patch.PatchError, "Unmanaged"):
            self.apply()
        manifest = patch.read_json(self.package / "app-manifest.json")
        manifest["files"]["web/NEW.js"] = "a" * 64
        patch.write_json(self.package / "app-manifest.json", manifest)
        with self.assertRaisesRegex(patch.PatchError, "Invalid manifest entry"):
            self.apply()

    def test_unsafe_paths_rejected(self):
        for name in ("../escape.py", "/tmp/escape", "C:/escape", "server/../../escape", "server\\x",
                     "runtime/python/python.exe", "projects/x", "voices.json", "web/NUL.txt", "web/x.",
                     "web/x:secret", "web//file", "web/./file"):
            with self.subTest(name=name), self.assertRaises(patch.PatchError):
                patch.target(self.app, name)

    def test_ownership_stop_failure_does_not_replace_files(self):
        self.runtime.stop.side_effect = patch.PatchError("ownership_mismatch")
        with self.assertRaisesRegex(patch.PatchError, "ownership_mismatch"):
            self.apply()
        self.assertEqual((self.app / "server/example.py").read_text(), "old")
        self.runtime.start.assert_not_called()

    def test_retired_source_is_preserved_and_restorable(self):
        old = self.app / "server/retired.py"
        old.write_text("retired")
        baseline = patch.read_json(self.app / "app-manifest.json")
        baseline["files"]["server/retired.py"] = patch.digest(old)
        patch.write_json(self.app / "app-manifest.json", baseline)
        backup = self.apply()
        self.assertFalse(old.exists())
        self.assertEqual((backup / "retired/server/retired.py").read_text(), "retired")
        patch.rollback(backup, self.app, self.runtime)
        self.assertEqual(old.read_text(), "retired")

    def test_interrupted_update_is_recoverable_and_blocks_second_patch(self):
        real_replace = patch.replace
        calls = 0

        def interrupted(source, target):
            nonlocal calls
            calls += 1
            real_replace(source, target)
            if calls == 2:
                raise KeyboardInterrupt("power loss")

        with mock.patch.object(patch, "replace", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.apply()
        backup = next((self.data / "updates").glob("*/transaction.json")).parent
        with self.assertRaisesRegex(patch.PatchError, "Unfinished update"):
            self.apply()
        patch.rollback(backup, self.app, self.runtime)
        self.assertEqual((self.app / "AI-Live-Studio.exe").read_text(), "old exe")
        self.assertEqual((self.app / "server/example.py").read_text(), "old")

    def test_rollback_refuses_later_edits_and_tampered_backup(self):
        backup = self.apply()
        (self.app / "server/example.py").write_text("later edit")
        with self.assertRaisesRegex(patch.PatchError, "Changed after update"):
            patch.rollback(backup, self.app, self.runtime)
        (self.app / "server/example.py").write_text("new")
        (backup / "files/server/example.py").write_text("corrupt backup")
        with self.assertRaisesRegex(patch.PatchError, "modified"):
            patch.rollback(backup, self.app, self.runtime)

    def test_status_exit_zero_is_not_a_health_pass(self):
        runtime = patch.Runtime(self.app, self.data)
        with mock.patch.object(runtime, "command", return_value=json.dumps({"health": {"tts": False}})), \
                mock.patch.object(patch.time, "monotonic", side_effect=[0, 1, 181]), \
                mock.patch.object(patch.time, "sleep"), \
                self.assertRaisesRegex(patch.PatchError, "health/ownership"):
            runtime.start()

    def test_packager_only_includes_tracked_code_and_all_shared_modules(self):
        launcher = self.app / "AI-Live-Studio.exe"
        destination = self.app.parent / "staged"
        manifest = packager.package(destination, launcher)
        for name in ("local_runtime/manager.py", "server/text_studio_entry.py", "recording_transcript/__init__.py",
                     "audio_cache/server.py", "AI-Live-Studio.exe", "windows_launcher.py"):
            self.assertIn(name, manifest["files"])
        self.assertNotIn("voices.json", manifest["files"])
        self.assertFalse(any(n.startswith("runtime/") for n in manifest["files"]))
        patch.verify(destination, manifest["files"])


if __name__ == "__main__":
    unittest.main()
