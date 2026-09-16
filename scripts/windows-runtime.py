"""Start/stop the Windows single-machine runtime.

The launcher owns only processes it starts and keeps models outside the
application directory.  It is also usable with ``--dry-run`` on non-Windows
hosts to inspect the resolved command lines.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import shutil
import subprocess
import sys
import time
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_runtime.file_lease import _pid_alive

DEFAULT_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "AI-Live-Studio"
DEFAULT_MODELS = Path(os.environ.get("AI_LIVE_STUDIO_MODELS", "D:/AI-Live-Studio-Models"))
RUNTIME_PORTS = (8765, 8766, 8000, 8770, 8771)
SERVICE_PORTS = {
    "ollama": 11435,
    "tts-gateway": 8765,
    "audio-cache": 8000,
    "recording-transcript": 8771,
    "text-studio": 8770,
}
SERVICE_MATCHERS = {
    "ollama": ("ollama.exe", "serve"),
    "tts-gateway": ("python.exe", "server/tts_gateway.py"),
    "audio-cache": ("python.exe", "scripts/audio-cache-server.py"),
    "recording-transcript": ("python.exe", "server/recording_transcript.py"),
    "text-studio": ("python.exe", "server/text_studio_entry.py"),
    "audio-client": ("python.exe", "windows_playback_service.py"),
}
HEALTH_ENDPOINTS = {
    "ollama": ("ollama", "http://127.0.0.1:11435/api/tags"),
    "tts": ("tts-gateway", "http://127.0.0.1:8765/health"),
    "cache": ("audio-cache", "http://127.0.0.1:8000/health"),
    "studio": ("text-studio", "http://127.0.0.1:8770/api/health"),
    "asr": ("recording-transcript", "http://127.0.0.1:8771/api/health"),
}


def _configured_models(data: Path, explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser()
    path = data / "config" / "models-path.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return DEFAULT_MODELS.expanduser()
    selected = value.get("models_dir") if isinstance(value, dict) else ""
    return Path(selected).expanduser() if isinstance(selected, str) and selected.strip() else DEFAULT_MODELS.expanduser()


def _paths(data: Path) -> tuple[Path, Path]:
    return data / "runtime" / "windows-processes.json", data / "logs"


def _component_dirs(bin_dir: Path) -> dict[str, Path]:
    return {
        "ollama": bin_dir / "ollama",
        "cosyvoice": bin_dir / "cosyvoice",
        "ffmpeg": bin_dir / "ffmpeg",
    }


def _record(name: str, pid: int, command: list[str], launcher_pid: int | None = None) -> dict[str, object]:
    executable = command[0] if command else SERVICE_MATCHERS[name][0]
    entrypoint = next((argument for argument in command[1:] if argument.endswith(".py")), "")
    if not entrypoint and name == "ollama":
        entrypoint = "serve"
    value: dict[str, object] = {
        "pid": pid,
        "expected_executable": executable,
        "expected_entrypoint": entrypoint,
        "command": command,
        "port": SERVICE_PORTS.get(name),
        "started_at": time.time(),
    }
    if launcher_pid and launcher_pid != pid:
        value["launcher_pid"] = launcher_pid
    return value


def _coerce_record(name: str, value: object) -> dict[str, object]:
    record = dict(value) if isinstance(value, dict) else {"pid": value}
    try:
        record["pid"] = int(record.get("pid", 0))
    except (TypeError, ValueError):
        record["pid"] = 0
    expected_executable, expected_entrypoint = SERVICE_MATCHERS.get(name, ("", ""))
    record.setdefault("expected_executable", expected_executable)
    record.setdefault("expected_entrypoint", expected_entrypoint)
    record.setdefault("port", SERVICE_PORTS.get(name))
    return record


def _load_processes(path: Path) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    services = value.get("services") if isinstance(value.get("services"), dict) else value
    return {
        name: _coerce_record(name, record)
        for name, record in services.items()
        if isinstance(name, str) and name != "version"
    }


def _load_pids(path: Path) -> dict[str, int]:
    """Backward-compatible PID view for older diagnostics and callers."""
    return {name: int(record["pid"]) for name, record in _load_processes(path).items() if int(record["pid"]) > 0}


def _running(pid: int) -> bool:
    return _pid_alive(pid)


def _write_processes(path: Path, records: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 2, "services": records}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_pids(path: Path, pids: dict[str, object]) -> None:
    """Write either legacy integer PIDs or full service records."""
    _write_processes(path, {name: _coerce_record(name, record) for name, record in pids.items()})


def _powershell() -> str:
    environment = getattr(os, "environ", {})
    candidates = [
        Path(environment.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe",
        Path("powershell.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "powershell.exe"


def _powershell_json(script: str) -> object:
    try:
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError):
        return None


def _process_info(pid: int) -> dict[str, str]:
    if not pid:
        return {}
    if os.name == "nt":
        value = _powershell_json(
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
            "if ($null -ne $p) { $p | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress }"
        )
        if isinstance(value, list):
            value = value[0] if value else {}
        if not isinstance(value, dict):
            return {}
        command_line = str(value.get("CommandLine") or "")
        executable = str(value.get("ExecutablePath") or "")
        if not executable and command_line:
            command_start = command_line.lstrip()
            executable = command_start.split('"', 2)[1] if command_start.startswith('"') else command_start.split(None, 1)[0]
        return {
            "pid": str(value.get("ProcessId", pid)),
            "parent_pid": str(value.get("ParentProcessId") or ""),
            "executable": executable,
            "command_line": command_line,
        }
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    command_line = result.stdout.strip()
    return {"pid": str(pid), "parent_pid": "", "executable": command_line.split(" ", 1)[0] if command_line else "", "command_line": command_line}


def _listener_pids(port: int) -> list[int]:
    if os.name == "nt":
        value = _powershell_json(
            f"Get-NetTCPConnection -State Listen -LocalPort {int(port)} -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty OwningProcess | ConvertTo-Json -Compress"
        )
        values = value if isinstance(value, list) else [value]
        result = []
        for item in values:
            try:
                pid = int(item)
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in result:
                result.append(pid)
        return result
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                continue
            if pid not in pids:
                pids.append(pid)
    return pids


def _normalise(value: str) -> str:
    return str(value or "").replace("/", "\\").strip().strip('"').casefold()


def _basename(value: str) -> str:
    return value.replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]


def _identity_matches(name: str, record: dict[str, object], info: dict[str, str]) -> bool:
    if not info:
        return False
    expected_executable, expected_entrypoint = SERVICE_MATCHERS.get(name, ("", ""))
    expected_executable = str(record.get("expected_executable") or expected_executable)
    expected_entrypoint = str(record.get("expected_entrypoint") or expected_entrypoint)
    actual_executable = _basename(info.get("executable", "")).casefold()
    if not actual_executable or actual_executable != _basename(expected_executable).casefold():
        return False
    command_line = _normalise(info.get("command_line", ""))
    expected = _normalise(expected_entrypoint)
    if not expected:
        return True
    if expected == "serve":
        return any(part == "serve" for part in command_line.split())
    return expected in command_line or _basename(expected) in command_line


def _inspect_process(name: str, record: dict[str, object]) -> dict[str, object]:
    pid = int(record.get("pid", 0) or 0)
    running = bool(pid and _running(pid))
    info = _process_info(pid) if running else {}
    listeners = _listener_pids(int(record["port"])) if record.get("port") else []
    matching_listener = next(
        (listener for listener in listeners if _identity_matches(name, record, _process_info(listener))),
        None,
    )
    identity_matches = running and _identity_matches(name, record, info)
    issues: list[str] = []
    if not pid:
        issues.append("not_tracked")
    elif not running:
        issues.append("stale_pid")
    elif not identity_matches:
        issues.append("ownership_mismatch")
    if listeners and (not identity_matches or pid not in listeners):
        issues.append("untracked_listener")
    if listeners and matching_listener is None:
        issues.append("unexpected_listener")
    return {
        "pid": pid or None,
        "tracked_pid": pid or None,
        "running": running,
        "owned": bool(identity_matches),
        "identity_matches": bool(identity_matches),
        "expected_executable": str(record.get("expected_executable", "")),
        "expected_entrypoint": str(record.get("expected_entrypoint", "")),
        "executable": info.get("executable", ""),
        "command_line": info.get("command_line", ""),
        "port": record.get("port"),
        "listener_pids": listeners,
        "listener_pid": matching_listener,
        "issues": issues,
    }


def _wait_for_owned_listener(name: str, record: dict[str, object], timeout: float = 15.0) -> int | None:
    port = record.get("port")
    if not port:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pid in _listener_pids(int(port)):
            if _identity_matches(name, record, _process_info(pid)):
                return pid
        time.sleep(0.2)
    return None


def _stop_processes(records: dict[str, object], timeout: float = 10.0) -> dict[str, dict[str, object]]:
    normalized = {name: _coerce_record(name, record) for name, record in records.items()}
    remaining = dict(normalized)
    for name, record in normalized.items():
        pid = int(record.get("pid", 0) or 0)
        if not _running(pid):
            remaining.pop(name, None)
            print(f"stopped {name} PID={pid} (already exited)")
            continue
        if not _identity_matches(name, record, _process_info(pid)):
            print(f"stop_skipped {name} PID={pid}: ownership_mismatch (process was not stopped)", file=sys.stderr)
            continue
        detail = ""
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=timeout,
                )
                if result.returncode:
                    detail = (result.stderr or result.stdout).strip()
            else:
                os.kill(pid, 15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = str(exc)
        deadline = time.monotonic() + timeout
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _running(pid):
            print(f"stop_failed {name} PID={pid}: {detail or 'process has not exited'}", file=sys.stderr)
            continue
        remaining.pop(name, None)
        print(f"stopped {name} PID={pid} (exit confirmed)")
    return remaining


def _untracked_listeners(records: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for name, port in SERVICE_PORTS.items():
        record = records.get(name, _coerce_record(name, {"pid": 0}))
        tracked_pid = int(record.get("pid", 0) or 0)
        for pid in _listener_pids(port):
            info = _process_info(pid)
            if pid == tracked_pid and _identity_matches(name, record, info):
                continue
            result.append({
                "service": name,
                "port": port,
                "pid": pid,
                "executable": info.get("executable", ""),
                "command_line": info.get("command_line", ""),
            })
    gateway = records.get("tts-gateway", {})
    owned_parents = {
        int(value)
        for value in (gateway.get("pid", 0), gateway.get("launcher_pid", 0))
        if str(value).isdigit() and int(value) > 0
    }
    for pid in _listener_pids(8766):
        info = _process_info(pid)
        if str(info.get("parent_pid", "")).isdigit() and int(info["parent_pid"]) in owned_parents:
            continue
        result.append({
            "service": "cosyvoice-engine",
            "port": 8766,
            "pid": pid,
            "executable": info.get("executable", ""),
            "command_line": info.get("command_line", ""),
        })
    return result


def _health_probe(service: str, url: str, timeout: float = 1.5) -> dict[str, object]:
    detail: dict[str, object] = {"ok": False, "service": service, "url": url, "timeout_seconds": timeout}
    try:
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=timeout) as response:
            detail.update({"ok": response.status == 200, "status": response.status})
            return detail
    except error.HTTPError as exc:
        detail.update({"status": exc.code, "exception_class": type(exc).__name__, "error": str(exc)})
    except (OSError, error.URLError, TimeoutError) as exc:
        detail.update({"exception_class": type(exc).__name__, "error": str(exc) or repr(exc)})
    return detail


def _url_ok(url: str, timeout: float = 1.5) -> bool:
    return bool(_health_probe("unknown", url, timeout)["ok"])


def _port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _model_errors(models: Path, bin_dir: Path) -> list[str]:
    errors: list[str] = []
    asr = models / "asr" / "Qwen3-ASR-1.7B"
    asr_config = asr / "config.json"
    if not asr.is_dir():
        errors.append(f"Qwen3-ASR-1.7B 未找到\n请将模型放到：{asr}")
    else:
        try:
            config = json.loads(asr_config.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            config = {}
        required_asr_files = ("config.json", "preprocessor_config.json", "tokenizer_config.json")
        if (config.get("model_type") != "qwen3_asr"
                or any(not (asr / name).is_file() for name in required_asr_files)
                or not any(asr.glob("*.safetensors"))):
            errors.append(f"Qwen3-ASR-1.7B 文件不完整\n请确认 {asr} 包含有效 config.json 和 safetensors 权重")

    tts = models / "tts"
    tts_model = tts / "CosyVoice3-2512_Q8_0.gguf"
    voices = tts / "voices.json"
    if not tts_model.is_file():
        errors.append(f"CosyVoice3 Q8 模型未找到\n请将模型放到：{tts_model}")
    if not voices.is_file():
        errors.append(f"voices.json 未找到\n请将音色配置放到：{voices}")
    else:
        try:
            voice_config = json.loads(voices.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            voice_config = {}
            errors.append(f"voices.json 无法读取\n请检查文件：{voices}")
        if not isinstance(voice_config, dict) or "default" not in voice_config:
            errors.append(f"voices.json 缺少 default 音色\n请检查文件：{voices}")
        elif isinstance(voice_config, dict):
            for voice_id, entry in voice_config.items():
                prompt = entry.get("prompt_speech") if isinstance(entry, dict) else ""
                prompt_path = Path(prompt).expanduser() if isinstance(prompt, str) else Path()
                prompt_candidates = [prompt_path] if prompt_path.is_absolute() else [ROOT / prompt_path, tts / prompt_path]
                if not any(path.is_file() for path in prompt_candidates):
                    errors.append(f"音色 {voice_id} 的 prompt_speech 未找到\n请检查：{prompt_candidates[-1]}")

    modelfile = models / "llm" / "Modelfile"
    ollama_store = models / "llm" / "ollama-store"
    if not modelfile.is_file() or not ollama_store.is_dir() or not any(ollama_store.iterdir()):
        errors.append(
            "Qwen3 8B / Ollama 模型未找到或不完整\n"
            f"请确认存在：{modelfile}\n以及非空目录：{ollama_store}"
        )

    components = _component_dirs(bin_dir)
    required = {
        "固定 Python Runtime": bin_dir.parent / "python" / ("python.exe" if os.name == "nt" else "python"),
        "Ollama": components["ollama"] / ("ollama.exe" if os.name == "nt" else "ollama"),
        "CosyVoice server": components["cosyvoice"] / ("cosyvoice-server.exe" if os.name == "nt" else "cosyvoice-server"),
        "CosyVoice DLL": components["cosyvoice"] / "cosyvoice.dll",
        "ONNX Runtime DLL": components["cosyvoice"] / "onnxruntime.dll",
        "FFmpeg": components["ffmpeg"] / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
        "ffprobe": components["ffmpeg"] / ("ffprobe.exe" if os.name == "nt" else "ffprobe"),
    }
    for label, path in required.items():
        if not path.is_file():
            errors.append(f"{label} 未随安装包提供\n请重新安装或检查程序目录：{path}")
    torch_lib = bin_dir.parent / "python" / "Lib" / "site-packages" / "torch" / "lib"
    for label, pattern, directories in (
        ("GGML DLL", "ggml*.dll", (components["cosyvoice"],)),
        ("CUDA runtime DLL", "cudart64_*.dll", (torch_lib, components["cosyvoice"])),
        ("cuBLAS DLL", "cublas64_*.dll", (torch_lib, components["cosyvoice"])),
    ):
        if not any(any(directory.glob(pattern)) for directory in directories):
            errors.append(f"{label} 未随安装包提供\n请重新安装或检查程序目录：{torch_lib}")
    return errors


def startup_errors(models: Path, data: Path, python: Path, bin_dir: Path, check_hardware: bool = True) -> list[str]:
    errors = _model_errors(models, bin_dir)
    if check_hardware and os.name == "nt":
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            candidates = (
                Path(os.environ.get("ProgramFiles", "")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
                Path(os.environ.get("ProgramW6432", "")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
            )
            nvidia_smi = next((str(path) for path in candidates if path.is_file()), "")
        try:
            result = subprocess.run(
                [nvidia_smi or "nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is None or result.returncode or not result.stdout.strip():
            errors.append("未检测到 NVIDIA GPU\n请安装 NVIDIA Driver 后重试；本安装包不需要 CUDA Toolkit")
        if python.is_file():
            try:
                cuda = subprocess.run(
                    [str(python), "-c", "import torch; print(torch.cuda.is_available())"],
                    capture_output=True, text=True, timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                cuda = None
            if cuda is None or cuda.returncode or cuda.stdout.strip().lower() != "true":
                errors.append("内置 PyTorch 无法使用 CUDA\n请更新 NVIDIA Driver；本安装包不需要 CUDA Toolkit")
    if not python.is_file():
        errors.append(f"固定 Python Runtime 未找到：{python}")
    if check_hardware and os.name == "nt":
        for port in RUNTIME_PORTS:
            if not _port_available(port):
                listeners = _listener_pids(port)
                detail = f"监听 PID：{', '.join(map(str, listeners))}" if listeners else "无法读取监听 PID"
                errors.append(f"端口 {port} 已被其他进程占用\n{detail}\n请关闭占用该端口的程序后重试")
    return errors


def commands(models: Path, data: Path, python: Path, bin_dir: Path) -> dict[str, tuple[list[str], Path]]:
    logs = data / "logs"
    components = _component_dirs(bin_dir)
    voices = models / "tts" / "voices.json"
    tts_model = models / "tts" / "CosyVoice3-2512_Q8_0.gguf"
    asr_model = models / "asr" / "Qwen3-ASR-1.7B"
    engine = components["cosyvoice"] / ("cosyvoice-server.exe" if os.name == "nt" else "cosyvoice-server")
    ollama = components["ollama"] / ("ollama.exe" if os.name == "nt" else "ollama")
    engine_backend = "cuda0" if os.name == "nt" else "cuda"
    return {
        "ollama": ([str(ollama), "serve"], components["ollama"]),
        "tts-gateway": ([str(python), str(ROOT / "server" / "tts_gateway.py"), "--host", "127.0.0.1", "--port", "8765", "--engine-url", "http://127.0.0.1:8766", "--engine-bin", str(engine), "--engine-model", str(tts_model), "--engine-backend", engine_backend, "--engine-log", str(logs / "cosyvoice-server.log"), "--audio-dir", str(data / "audio"), "--tts-cache-dir", str(data / "tts-cache"), "--voices-config", str(voices)], ROOT),
        "audio-cache": ([str(python), str(ROOT / "scripts" / "audio-cache-server.py"), "--host", "127.0.0.1", "--port", "8000", "--root", str(data / "audio-cache")], ROOT),
        "recording-transcript": ([str(python), str(ROOT / "server" / "recording_transcript.py"), "--host", "127.0.0.1", "--port", "8771", "--model", str(asr_model)], ROOT),
        "text-studio": ([str(python), str(ROOT / "server" / "text_studio_entry.py"), "--host", "127.0.0.1", "--port", "8770", "--tts-gateway-url", "http://127.0.0.1:8765", "--audio-cache-url", "http://127.0.0.1:8000"], ROOT),
        "audio-client": ([str(python), str(ROOT / "windows_playback_service.py")], ROOT),
    }


def start(args: argparse.Namespace) -> int:
    if os.name != "nt" and not args.dry_run:
        print("windows-runtime.py must run on Windows; use --dry-run here", file=sys.stderr)
        return 2
    data = Path(args.data).expanduser()
    models = _configured_models(data, args.models)
    python = Path(args.python or sys.executable).expanduser()
    bin_dir = Path(args.bin_dir or ROOT / "runtime" / "bin").expanduser()
    entries = commands(models, data, python, bin_dir)
    if not args.dry_run:
        errors = startup_errors(models, data, python, bin_dir)
        if errors:
            print("无法启动 AI Live Studio：\n\n" + "\n\n".join(errors), file=sys.stderr)
            return 1
    if args.dry_run:
        for name, (argv, cwd) in entries.items():
            print(name, json.dumps(argv, ensure_ascii=False), "cwd=", cwd)
        return 0
    if not data.is_dir():
        data.mkdir(parents=True, exist_ok=True)
    process_file, logs = _paths(data)
    existing = _load_processes(process_file)
    for name, record in existing.items():
        state = _inspect_process(name, record)
        if state["owned"]:
            print("AI Live Studio runtime is already running", file=sys.stderr)
            return 1
        if state["running"] or state["listener_pids"]:
            print(
                f"无法启动：{name} 存在未归属的运行进程或监听器；"
                f"请先处理 windows-processes.json / status 中的 ownership mismatch",
                file=sys.stderr,
            )
            return 1
    logs.mkdir(parents=True, exist_ok=True)
    from local_runtime.settings import tts_secret_store
    key_store = tts_secret_store(data)
    try:
        api_key = key_store.get()
    except Exception:
        api_key = ""
    if not api_key:
        api_key = secrets.token_urlsafe(32)
        key_store.set(api_key)

    env = os.environ.copy()
    env.update({
        "AI_LIVE_STUDIO_DATA": str(data),
        "AI_LIVE_STUDIO_VERSION": "1.0.0",
        "AI_LIVE_STUDIO_GPU_LOCK": str(data / "runtime" / "gpu-owner.json"),
        "TTS_API_KEY": api_key,
        "WINDOWS_SINGLE_MACHINE": "1",
        "OLLAMA_HOST": "127.0.0.1:11435",
        "OLLAMA_MODELS": str(models / "llm" / "ollama-store"),
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "TTS_OLLAMA_URL": "http://127.0.0.1:11435",
        "TTS_OLLAMA_MODEL": "qwen3:8b",
        "RECORDING_TRANSCRIPT_BACKEND": "cuda",
        "PATH": os.pathsep.join(str(path) for path in (*_component_dirs(bin_dir).values(), bin_dir, python.parent / "Lib" / "site-packages" / "torch" / "lib")) + os.pathsep + env.get("PATH", ""),
        "AI_AUDIO_CLIENT_CONFIG": str(data / "config" / "audio-client.json"),
        "AI_AUDIO_AUTOPLAY": "1",
    })
    client_config = data / "config" / "audio-client.json"
    client_config.parent.mkdir(parents=True, exist_ok=True)
    if not client_config.exists():
        client_config.write_text(json.dumps({
            "server": "http://127.0.0.1:8000", "cache_dir": str(data / "cache"),
            "poll_interval": 1, "api_key": "", "timeout": 10,
            "strict_session": True, "session_id": "", "startup_buffer_seconds": 30,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    started: dict[str, dict[str, object]] = {}
    try:
        for name in ("ollama", "tts-gateway", "audio-cache", "recording-transcript", "text-studio", "audio-client"):
            argv, cwd = entries[name]
            log = (logs / f"{name}.log").open("ab")
            try:
                proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            finally:
                log.close()
            started[name] = _record(name, proc.pid, argv)
        # A wrapper can exit after handing the socket to a child process. For
        # port-backed services, store the identity that actually owns the
        # listening socket and retain the original launcher PID for diagnosis.
        for name, record in started.items():
            listener_pid = _wait_for_owned_listener(name, record)
            if listener_pid and listener_pid != record["pid"]:
                record["launcher_pid"] = record["pid"]
                record["pid"] = listener_pid
        _write_processes(process_file, started)
    except Exception as exc:
        _write_processes(process_file, _stop_processes(started))
        print(f"runtime start failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(started, ensure_ascii=False))
    return 0


def stop(args: argparse.Namespace) -> int:
    path, _ = _paths(Path(args.data).expanduser())
    records = _load_processes(path)
    remaining = _stop_processes(records)
    untracked = _untracked_listeners(records) if os.name == "nt" else []
    for listener in untracked:
        print(
            f"untracked listener detected service={listener['service']} port={listener['port']} "
            f"PID={listener['pid']} executable={listener['executable']} "
            f"command={listener['command_line']}",
            file=sys.stderr,
        )
    if records or path.exists():
        _write_processes(path, remaining)
    return 1 if remaining or untracked else 0


def status(args: argparse.Namespace) -> int:
    data = Path(args.data).expanduser()
    path, _ = _paths(data)
    records = _load_processes(path)
    process_names = list(SERVICE_MATCHERS)
    processes = {
        name: _inspect_process(name, records.get(name, _coerce_record(name, {"pid": 0})))
        for name in process_names
    }
    health_details = {
        name: _health_probe(service, url)
        for name, (service, url) in HEALTH_ENDPOINTS.items()
    }
    listeners = {
        str(port): [
            {"pid": pid, **_process_info(pid)}
            for pid in _listener_pids(port)
        ]
        for port in RUNTIME_PORTS + (11435,)
    }
    issues = [
        {"service": name, "issues": state["issues"], "pid": state["pid"], "listener_pids": state["listener_pids"]}
        for name, state in processes.items() if state["issues"]
    ]
    issues.extend(
        {"service": name, "issues": ["health_failed"], "detail": detail}
        for name, detail in health_details.items() if not detail["ok"]
    )
    print(json.dumps({
        "processes": processes,
        "health": {name: bool(detail["ok"]) for name, detail in health_details.items()},
        "health_details": health_details,
        "listeners": listeners,
        "issues": issues,
    }, ensure_ascii=False, indent=2))
    return 0


def check(args: argparse.Namespace) -> int:
    data = Path(args.data).expanduser()
    models = _configured_models(data, args.models)
    python = Path(args.python or sys.executable).expanduser()
    bin_dir = Path(args.bin_dir or ROOT / "runtime" / "bin").expanduser()
    errors = startup_errors(models, data, python, bin_dir, check_hardware=not args.dry_run)
    print(json.dumps({
        "ok": not errors,
        "models": str(models),
        "data": str(data),
        "errors": errors,
        "ports": list(RUNTIME_PORTS),
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def import_llm(args: argparse.Namespace) -> int:
    if os.name != "nt" and not args.dry_run:
        print("windows-runtime.py must run on Windows; use --dry-run here", file=sys.stderr)
        return 2
    models = Path(args.models).expanduser()
    bin_dir = Path(args.bin_dir or ROOT / "runtime" / "bin").expanduser()
    ollama = bin_dir / ("ollama.exe" if os.name == "nt" else "ollama")
    modelfile = models / "llm" / "Modelfile"
    if not modelfile.is_file() and not args.dry_run:
        print(f"Missing {modelfile}; the offline model package must provide a Modelfile", file=sys.stderr)
        return 1
    command = [str(ollama), "create", "qwen3:8b", "-f", str(modelfile)]
    if args.dry_run:
        print(json.dumps(command, ensure_ascii=False))
        return 0
    env = os.environ.copy()
    env.update({"OLLAMA_HOST": "127.0.0.1:11435", "OLLAMA_MODELS": str(models / "llm" / "ollama-store"), "OLLAMA_NO_CLOUD": "1"})
    result = subprocess.run(command, env=env, cwd=str(modelfile.parent), text=True)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "start", "stop", "status", "import-llm"))
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--models", default=str(DEFAULT_MODELS))
    parser.add_argument("--python", default="")
    parser.add_argument("--bin-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return {"check": check, "start": start, "stop": stop, "status": status, "import-llm": import_llm}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
