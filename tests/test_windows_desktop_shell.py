"""Regression checks for the Windows desktop shell contract (no Windows GUI required)."""
from pathlib import Path
import ast
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from desktop_shell import run_desktop

ROOT = Path(__file__).resolve().parents[1]


class DesktopShellContractTests(unittest.TestCase):
    def test_launcher_does_not_open_external_browser(self):
        source = (ROOT / "windows_launcher.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertNotIn("webbrowser.open", source)
        self.assertIn("run_desktop(STUDIO_URL, stop_runtime)", source)
        self.assertIn('root.withdraw()', source)
        self.assertTrue(any(isinstance(node, ast.FunctionDef) and node.name == "_start" for node in tree.body))

    def test_close_hides_and_only_successful_exit_destroys(self):
        class Event:
            def __iadd__(self, callback):
                self.callback = callback
                return self
        event = Event()
        window = Mock(events=SimpleNamespace(closing=event))
        tray = Mock()
        stop = Mock(side_effect=[False, True])
        menu_items = {}
        def menu_item(label, action, **kwargs):
            menu_items[label] = action
            return action
        menu = Mock(side_effect=lambda *items: items)
        pystray = SimpleNamespace(Icon=Mock(return_value=tray), Menu=menu, MenuItem=menu_item)
        def start(**kwargs):
            self.assertIs(event.callback(), False)
            window.hide.assert_called_once()
            stop.assert_not_called()
            menu_items["打开工作台"]()
            window.show.assert_called_once()
            exit_action = menu_items["确认退出并停止后台"]
            exit_action(tray)
            window.destroy.assert_not_called()
            tray.stop.assert_not_called()
            exit_action(tray)
            window.destroy.assert_called_once()
            self.assertIs(event.callback(), True)
        webview = SimpleNamespace(create_window=Mock(return_value=window), start=start)
        with patch.dict(sys.modules, {"webview": webview, "pystray": pystray,
                                     "PIL": SimpleNamespace(Image=Mock(), ImageDraw=Mock())}):
            run_desktop('http://127.0.0.1:8770', stop)

    def test_bundle_carries_desktop_dependencies(self):
        build = (ROOT / "build/windows/build.ps1").read_text(encoding="utf-8")
        spec = (ROOT / "build/windows/AI-Live-Studio.spec").read_text(encoding="utf-8")
        package = (ROOT / "build/windows/package_app.py").read_text(encoding="utf-8")
        # Require standalone executable commands, not names inside comments.
        lines = [line.strip() for line in build.splitlines()]
        install = next(line for line in lines if line.startswith('& $HostPython -m pip install "pywebview'))
        for name in ("pywebview", "pystray", "Pillow", "cryptography"):
            self.assertIn(name, install)
        compile_line = next(line for line in lines if line.startswith('& $HostPython -m PyInstaller '))
        self.assertIn('--distpath $launcherOutput', compile_line)
        self.assertIn('if ($LASTEXITCODE -ne 0) { throw "PyInstaller launcher build failed" }', lines)
        self.assertIn('if (-not (Test-Path -LiteralPath $launcherExe -PathType Leaf)) { throw "PyInstaller did not produce the launcher executable" }', lines)
        package_line = next(line for line in lines if line.startswith('& $HostPython (Join-Path $PSScriptRoot "package_app.py")'))
        self.assertIn('--launcher $launcherExe', package_line)
        runtime_install = next(line for line in lines if line.startswith('-r (Join-Path $Root "scripts'))
        self.assertTrue(runtime_install.endswith(' cryptography'))
        smoke = next(line for line in lines if line.startswith('& $bundledPython -c "from cryptography'))
        self.assertIn('.verify(key.sign(', smoke)
        self.assertIn("webview.platforms.edgechromium", spec)
        self.assertIn("desktop_shell.py", package)


if __name__ == "__main__":
    unittest.main()
