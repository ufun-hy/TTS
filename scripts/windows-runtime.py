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
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_runtime.file_lease import _pid_alive

DEFAULT_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "AI-Live-Studio"
DEFAULT_MODELS = Path(os.environ.get("AI_LIVE_STUDIO_MODELS", "D:/AI-Live-Studio-Models"))
RUNTIME_PORTS = (8765, 8766, 8000, 8770, 8771)


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


def _load_pids(path: Path) -> dict[str, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return {name: int(pid) for name, pid in value.items() if isinstance(name, str) and str(pid).isdigit()}


def _running(pid: int) -> bool:
    return _pid_alive(pid)


def _stop_processes(pids: dict[str, int], timeout: float = 10.0) -> dict[str, int]:
    remaining = dict(pids)
    for name, pid in pids.items():
        if _running(pid):
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


def _write_pids(path: Path, pids: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(pids, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _url_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except OSError:
        return False


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
                errors.append(f"端口 {port} 已被其他进程占用\n请关闭占用该端口的程序后重试")
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
    pids = _load_pids(process_file)
    if any(_running(pid) for pid in pids.values()):
        print("AI Live Studio runtime is already running", file=sys.stderr)
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
    started: dict[str, int] = {}
    try:
        for name in ("ollama", "tts-gateway", "audio-cache", "recording-transcript", "text-studio", "audio-client"):
            argv, cwd = entries[name]
            log = (logs / f"{name}.log").open("ab")
            try:
                proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            finally:
                log.close()
            started[name] = proc.pid
        _write_pids(process_file, started)
    except Exception as exc:
        _write_pids(process_file, _stop_processes(started))
        print(f"runtime start failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(started, ensure_ascii=False))
    return 0


def stop(args: argparse.Namespace) -> int:
    path, _ = _paths(Path(args.data).expanduser())
    pids = _load_pids(path)
    remaining = _stop_processes(pids)
    if pids or path.exists():
        _write_pids(path, remaining)
    return 1 if remaining else 0


def status(args: argparse.Namespace) -> int:
    data = Path(args.data).expanduser()
    path, _ = _paths(data)
    pids = _load_pids(path)
    endpoints = {"ollama": "http://127.0.0.1:11435/api/tags", "tts": "http://127.0.0.1:8765/health", "cache": "http://127.0.0.1:8000/health", "studio": "http://127.0.0.1:8770/api/health", "asr": "http://127.0.0.1:8771/api/health"}
    print(json.dumps({"processes": {name: {"pid": pid, "running": _running(pid)} for name, pid in pids.items()}, "health": {name: _url_ok(url) for name, url in endpoints.items()}}, ensure_ascii=False, indent=2))
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
