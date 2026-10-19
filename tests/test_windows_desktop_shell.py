"""Regression checks for the Windows desktop shell contract (no Windows GUI required)."""
from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DesktopShellContractTests(unittest.TestCase):
    def test_launcher_does_not_open_external_browser(self):
        source = (ROOT / "windows_launcher.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertNotIn("webbrowser.open", source)
        self.assertIn("run_desktop(STUDIO_URL, stop_runtime)", source)
        self.assertIn('root.withdraw()', source)
        self.assertTrue(any(isinstance(node, ast.FunctionDef) and node.name == "_start" for node in tree.body))

    def test_close_hides_without_stopping_runtime(self):
        source = (ROOT / "desktop_shell.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("window.hide()", source)
        self.assertIn("window.events.closing += on_close", source)
        self.assertIn("if not stop_runtime():", source)
        self.assertIn('webview.start(gui="edgechromium"', source)

    def test_bundle_carries_desktop_dependencies(self):
        build = (ROOT / "build/windows/build.ps1").read_text(encoding="utf-8")
        spec = (ROOT / "build/windows/AI-Live-Studio.spec").read_text(encoding="utf-8")
        package = (ROOT / "build/windows/package_app.py").read_text(encoding="utf-8")
        for name in ("pywebview", "pystray", "Pillow"):
            self.assertIn(name, build)
        self.assertIn("webview.platforms.edgechromium", spec)
        self.assertIn("desktop_shell.py", package)


if __name__ == "__main__":
    unittest.main()
