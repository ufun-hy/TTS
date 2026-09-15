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
import subprocess
import sys
import time
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_runtime.file_lease import _pid_alive

DEFAULT_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "AI-Live-Studio"
DEFAULT_MODELS = Path(os.environ.get("AI_LIVE_STUDIO_MODELS", "D:/AI-Live-Studio-Models"))


def _paths(data: Path) -> tuple[Path, Path]:
    return data / "runtime" / "windows-processes.json", data / "logs"


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


def commands(models: Path, data: Path, python: Path, bin_dir: Path) -> dict[str, tuple[list[str], Path]]:
    logs = data / "logs"
    voices = models / "tts" / "voices.json"
    tts_model = models / "tts" / "CosyVoice3-2512_Q8_0.gguf"
    asr_model = models / "asr" / "Qwen3-ASR-1.7B"
    engine = bin_dir / ("cosyvoice-server.exe" if os.name == "nt" else "cosyvoice-server")
    ollama = bin_dir / ("ollama.exe" if os.name == "nt" else "ollama")
    return {
        "ollama": ([str(ollama), "serve"], bin_dir),
        "tts-gateway": ([str(python), str(ROOT / "server" / "tts_gateway.py"), "--host", "127.0.0.1", "--port", "8765", "--engine-url", "http://127.0.0.1:8766", "--engine-bin", str(engine), "--engine-model", str(tts_model), "--engine-backend", "cuda", "--engine-log", str(logs / "cosyvoice-server.log"), "--audio-dir", str(data / "audio"), "--tts-cache-dir", str(data / "tts-cache"), "--voices-config", str(voices)], ROOT),
        "audio-cache": ([str(python), str(ROOT / "scripts" / "audio-cache-server.py"), "--host", "127.0.0.1", "--port", "8000", "--root", str(data / "audio-cache")], ROOT),
        "recording-transcript": ([str(python), str(ROOT / "server" / "recording_transcript.py"), "--host", "127.0.0.1", "--port", "8771", "--model", str(asr_model)], ROOT),
        "text-studio": ([str(python), str(ROOT / "server" / "text_studio_entry.py"), "--host", "127.0.0.1", "--port", "8770", "--tts-gateway-url", "http://127.0.0.1:8765", "--audio-cache-url", "http://127.0.0.1:8000"], ROOT),
        "audio-client": ([str(python), str(ROOT / "windows_client.py")], ROOT),
    }


def start(args: argparse.Namespace) -> int:
    if os.name != "nt" and not args.dry_run:
        print("windows-runtime.py must run on Windows; use --dry-run here", file=sys.stderr)
        return 2
    data, models = Path(args.data).expanduser(), Path(args.models).expanduser()
    python = Path(args.python or sys.executable).expanduser()
    bin_dir = Path(args.bin_dir or ROOT / "runtime" / "bin").expanduser()
    entries = commands(models, data, python, bin_dir)
    required = [models / "tts" / "CosyVoice3-2512_Q8_0.gguf", models / "tts" / "voices.json", models / "asr" / "Qwen3-ASR-1.7B"]
    missing = [str(path) for path in required if not path.exists()]
    if missing and not args.dry_run:
        print("Missing external model assets:\n" + "\n".join(missing), file=sys.stderr)
        return 1
    if args.dry_run:
        for name, (argv, cwd) in entries.items():
            print(name, json.dumps(argv, ensure_ascii=False), "cwd=", cwd)
        return 0

    process_file, logs = _paths(data)
    pids = _load_pids(process_file)
    if any(_running(pid) for pid in pids.values()):
        print("AI Live Studio runtime is already running", file=sys.stderr)
        return 1
    data.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    from local_runtime.settings import secret_store
    key_store = secret_store(data)
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
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
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
    _write_pids(path, remaining)
    return 1 if remaining else 0


def status(args: argparse.Namespace) -> int:
    data = Path(args.data).expanduser()
    path, _ = _paths(data)
    pids = _load_pids(path)
    endpoints = {"ollama": "http://127.0.0.1:11435/api/tags", "tts": "http://127.0.0.1:8765/health", "cache": "http://127.0.0.1:8000/health", "studio": "http://127.0.0.1:8770/api/health", "asr": "http://127.0.0.1:8771/api/health"}
    print(json.dumps({"processes": {name: {"pid": pid, "running": _running(pid)} for name, pid in pids.items()}, "health": {name: _url_ok(url) for name, url in endpoints.items()}}, ensure_ascii=False, indent=2))
    return 0


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
    parser.add_argument("command", choices=("start", "stop", "status", "import-llm"))
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--models", default=str(DEFAULT_MODELS))
    parser.add_argument("--python", default="")
    parser.add_argument("--bin-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return {"start": start, "stop": stop, "status": status, "import-llm": import_llm}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
