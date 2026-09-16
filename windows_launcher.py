"""User-facing Windows entrypoint for the bundled AI Live Studio runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser


STUDIO_URL = "http://127.0.0.1:8770/"


def install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_root() -> Path:
    value = os.environ.get("AI_LIVE_STUDIO_DATA", "")
    if value:
        return Path(value).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")
    return Path(local_app_data) / "AI-Live-Studio"


def _runtime_command(command: str, data: Path) -> list[str]:
    root = install_root()
    python = root / "runtime" / "python" / "python.exe"
    script = root / "scripts" / "windows-runtime.py"
    return [str(python), str(script), command, "--data", str(data)]


def _run_runtime(command: str, data: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    args = _runtime_command(command, data)
    try:
        return subprocess.run(
            args,
            cwd=str(install_root()),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=flags,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, exc.stdout or "", f"Runtime command timed out: {command}")


def _json_output(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_models_path(data: Path, selected: str) -> None:
    path = data / "config" / "models-path.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"models_dir": selected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _choose_models(data: Path, parent: tk.Misc | None = None) -> bool:
    selected = filedialog.askdirectory(
        title="选择 AI-Live-Studio-Models 模型目录",
        mustexist=True,
        parent=parent,
    )
    if not selected:
        return False
    _save_models_path(data, selected)
    return True


def _show_error(title: str, detail: str, parent: tk.Misc | None = None) -> None:
    messagebox.showerror(title, detail, parent=parent)


def _check_and_maybe_choose(data: Path, parent: tk.Misc) -> bool:
    result = _run_runtime("check", data)
    if result.returncode == 0:
        return True
    payload = _json_output(result)
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    has_model_error = any("模型" in str(item) or "voices.json" in str(item) for item in errors)
    if has_model_error and _choose_models(data, parent):
        result = _run_runtime("check", data)
        if result.returncode == 0:
            return True
        payload = _json_output(result)
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    detail = "\n\n".join(str(item) for item in errors)
    if not detail:
        detail = (result.stderr or result.stdout or "启动检查失败").strip()
    _show_error("AI Live Studio 无法启动", detail, parent)
    return False


def _wait_for_runtime(data: Path, parent: tk.Misc) -> bool:
    deadline = time.monotonic() + 60
    last = ""
    while time.monotonic() < deadline:
        result = _run_runtime("status", data, timeout=10)
        payload = _json_output(result)
        health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
        if all(health.get(name) is True for name in ("ollama", "tts", "cache", "studio", "asr")):
            return True
        last = result.stdout.strip() or result.stderr.strip()
        processes = payload.get("processes") if isinstance(payload.get("processes"), dict) else {}
        if processes and not any(item.get("running") for item in processes.values() if isinstance(item, dict)):
            break
        time.sleep(0.5)
    detail = "Runtime 启动后未完成健康检查。"
    if last:
        detail += f"\n\n{last[-3000:]}"
    detail += f"\n\n日志目录：{data / 'logs'}"
    _show_error("AI Live Studio 启动失败", detail, parent)
    return False


def _start(data: Path, parent: tk.Misc) -> int:
    current = _json_output(_run_runtime("status", data, timeout=10))
    current_health = current.get("health") if isinstance(current.get("health"), dict) else {}
    current_processes = current.get("processes") if isinstance(current.get("processes"), dict) else {}
    studio_process = current_processes.get("text-studio") if isinstance(current_processes.get("text-studio"), dict) else {}
    if current_health.get("studio") is True and studio_process.get("owned") is True:
        webbrowser.open(STUDIO_URL)
        return 0
    if not _check_and_maybe_choose(data, parent):
        return 1
    result = _run_runtime("start", data, timeout=60)
    if result.returncode:
        _show_error("AI Live Studio 启动失败", (result.stderr or result.stdout).strip(), parent)
        return result.returncode or 1
    if not _wait_for_runtime(data, parent):
        return 1
    webbrowser.open(STUDIO_URL)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data = data_root()
    root = tk.Tk()
    root.withdraw()
    try:
        if argv == ["--choose-models"]:
            return 0 if _choose_models(data, root) else 1
        if argv == ["--stop"]:
            result = _run_runtime("stop", data)
            if result.returncode:
                _show_error("AI Live Studio 停止失败", (result.stderr or result.stdout).strip(), root)
            return result.returncode
        if argv == ["--status"]:
            result = _run_runtime("status", data)
            if result.returncode:
                _show_error("AI Live Studio 状态检查失败", (result.stderr or result.stdout).strip(), root)
            else:
                _show_error("AI Live Studio 状态", result.stdout.strip() or "Runtime 未运行", root)
            return result.returncode
        if argv:
            _show_error("AI Live Studio", "不支持的启动参数。", root)
            return 2
        return _start(data, root)
    finally:
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
