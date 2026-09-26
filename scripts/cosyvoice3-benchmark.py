#!/usr/bin/env python3
"""No-cache Windows CosyVoice benchmark using the bundled ManagedEngine."""

from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.request import urlopen
import uuid
import wave


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _wav_seconds(audio: bytes) -> float:
    if not audio.startswith(b"RIFF"):
        raise ValueError("CosyVoice response is not a RIFF/WAV file")
    with wave.open(io.BytesIO(audio), "rb") as handle:
        if handle.getcomptype() != "NONE" or handle.getnframes() <= 0:
            raise ValueError("CosyVoice returned empty or compressed WAV audio")
        return handle.getnframes() / handle.getframerate()


def _gateway_cache_stats() -> dict | None:
    try:
        with urlopen("http://127.0.0.1:8765/health", timeout=3) as response:
            body = json.load(response)
        stats = body.get("tts_cache") if isinstance(body, dict) else None
        return stats if isinstance(stats, dict) else None
    except Exception:
        return None


class _WindowsMemory:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("the benchmark runner must execute on Windows")
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        class ProcessCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        self._status_type = MemoryStatus
        self._counter_type = ProcessCounters
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        self._kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessCounters),
            wintypes.DWORD,
        ]
        self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    def sample(self, pid: int | None) -> tuple[int, int, int]:
        status = self._status_type()
        status.dwLength = ctypes.sizeof(status)
        if not self._kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        total = int(status.ullTotalPhys // (1024 * 1024))
        available = int(status.ullAvailPhys // (1024 * 1024))
        process_mb = 0
        if pid:
            handle = self._kernel.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
            if handle:
                try:
                    counters = self._counter_type()
                    counters.cb = ctypes.sizeof(counters)
                    if self._psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                        process_mb = int(counters.WorkingSetSize // (1024 * 1024))
                finally:
                    self._kernel.CloseHandle(handle)
        return total - available, available, process_mb


class _Telemetry:
    def __init__(self, engine_pid) -> None:
        self.engine_pid = engine_pid
        self.memory = _WindowsMemory()
        self.lock = threading.Lock()
        self.samples: list[dict] = []
        self.iteration_samples: list[dict] = []
        self.iteration = 0
        self.latest: dict = {}
        self.unsafe_reason = ""
        self._recent_vram: deque[int] = deque(maxlen=120)
        self._growth_guard = False
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def start(self, nvidia_smi: str) -> None:
        self._process = subprocess.Popen(
            [
                nvidia_smi,
                "--query-gpu=timestamp,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
                "--loop-ms=500",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_samples, name="gpu-sampler", daemon=True)
        self._reader.start()
        deadline = time.monotonic() + 15
        while not self.latest and time.monotonic() < deadline and not self.unsafe_reason:
            time.sleep(0.1)
        if not self.latest:
            raise RuntimeError(self.unsafe_reason or "nvidia-smi did not produce a sample")

    def _read_samples(self) -> None:
        assert self._process and self._process.stdout
        try:
            for line in self._process.stdout:
                if self._stop.is_set():
                    return
                columns = next(csv.reader([line]))
                if len(columns) < 5:
                    continue
                try:
                    gpu = {
                        "timestamp": columns[0].strip(),
                        "gpu_utilization": int(columns[1].strip()),
                        "gpu_memory_used_mb": int(columns[2].strip()),
                        "gpu_memory_total_mb": int(columns[3].strip()),
                        "gpu_temperature_c": int(columns[4].strip()),
                    }
                except ValueError:
                    continue
                system_used, system_available, process_mb = self.memory.sample(self.engine_pid())
                sample = {
                    **gpu,
                    "system_memory_used_mb": system_used,
                    "system_memory_available_mb": system_available,
                    "process_memory_mb": process_mb,
                }
                with self.lock:
                    self.latest = sample
                    self.samples.append(sample)
                    self.iteration_samples.append(sample)
                    if self._growth_guard:
                        self._recent_vram.append(gpu["gpu_memory_used_mb"])
                    if gpu["gpu_temperature_c"] >= 85:
                        self.unsafe_reason = f"GPU temperature reached {gpu['gpu_temperature_c']}C"
                    elif gpu["gpu_memory_used_mb"] >= 11264:
                        self.unsafe_reason = f"GPU VRAM reached {gpu['gpu_memory_used_mb']} MiB"
                    elif system_available < 4096:
                        self.unsafe_reason = f"available system RAM fell to {system_available} MiB"
                    elif len(self._recent_vram) == self._recent_vram.maxlen:
                        window = list(self._recent_vram)
                        if max(window) - min(window) >= 1536 and window[-1] >= max(window) - 128:
                            self.unsafe_reason = "GPU VRAM grew by 1.5 GiB in a one-minute window and did not fall"
        except Exception as exc:  # pragma: no cover - surfaced in the status report
            with self.lock:
                if not self._stop.is_set():
                    self.unsafe_reason = f"GPU/RAM sampler failed: {exc}"

    def begin_iteration(self, iteration: int) -> None:
        with self.lock:
            self.iteration = iteration
            self.iteration_samples = []

    def enable_growth_guard(self) -> None:
        with self.lock:
            self._recent_vram.clear()
            self._growth_guard = True

    def end_iteration(self) -> dict:
        with self.lock:
            samples = list(self.iteration_samples) or ([self.latest] if self.latest else [])
        if not samples:
            return {}
        return {
            **samples[-1],
            "gpu_utilization": round(sum(item["gpu_utilization"] for item in samples) / len(samples), 1),
            "gpu_memory_peak_mb": max(item["gpu_memory_used_mb"] for item in samples),
            "gpu_temperature_peak_c": max(item["gpu_temperature_c"] for item in samples),
            "system_memory_peak_mb": max(item["system_memory_used_mb"] for item in samples),
            "process_memory_peak_mb": max(item["process_memory_mb"] for item in samples),
        }

    def close(self) -> None:
        self._stop.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        if self._reader:
            self._reader.join(timeout=3)


def _p95(values: list[float]) -> float:
    return _percentile(values, 0.95)


def _write_status(path: Path, state: str, run_id: str, target: int, completed: int,
                  actual: int, load_seconds: float, last_rtf: float | None, error: str = "") -> None:
    _write_json(path, {
        "task_id": "cosyvoice3-windows-no-cache-benchmark-002",
        "run_id": run_id,
        "state": state,
        "target_iterations": target,
        "completed_iterations": completed,
        "actual_engine_inferences": actual,
        "model_load_seconds": round(load_seconds, 3),
        "last_rtf": round(last_rtf, 4) if last_rtf is not None else None,
        "updated_at": _now(),
        "error": error,
    })


def _benchmark_text(iteration: int) -> str:
    return f"这是第 {iteration:06d} 次 CosyVoice3 Windows 持续推理压力测试。"


def run_benchmark(args: argparse.Namespace) -> int:
    if os.name != "nt":
        raise RuntimeError("this benchmark must run in the installed Windows Python environment")
    app_root = args.app_root.resolve()
    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"benchmark output already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    config_path = data_dir / "config" / "models-path.json"
    configured = {}
    if config_path.is_file():
        configured = json.loads(config_path.read_text(encoding="utf-8"))
    models_dir = Path(configured.get("models_dir") or r"D:\AI-Live-Studio-Models").expanduser()
    if not models_dir.is_absolute():
        models_dir = (data_dir / models_dir).resolve()
    model = models_dir / "tts" / "CosyVoice3-2512_Q8_0.gguf"
    voices_config = models_dir / "tts" / "voices.json"
    engine_bin = app_root / "runtime" / "bin" / "cosyvoice" / "cosyvoice-server.exe"
    prompt = json.loads(voices_config.read_text(encoding="utf-8"))["default"]["prompt_speech"]
    prompt_path = Path(prompt)
    if not prompt_path.is_absolute():
        prompt_path = voices_config.parent / prompt_path
    for path in (model, voices_config, engine_bin, prompt_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if prompt_path.name != "speaker_c_reviewed.gguf":
        raise RuntimeError(f"default voice is not the reviewed speaker_c prompt: {prompt_path}")
    if not app_root.is_dir():
        raise FileNotFoundError(app_root)

    sys.path.insert(0, str(app_root))
    from local_runtime import FileGpuLease
    from server.engine_runtime import ManagedEngine
    from server.tts_gateway import VoiceStore, _engine_request_text

    data_runtime = data_dir / "runtime"
    log_path = output_dir / "cosyvoice-engine.log"
    voices = VoiceStore(app_root, voices_config, f"http://127.0.0.1:{args.port}")
    engine_url = f"http://127.0.0.1:{args.port}"
    engine_command = [
        str(engine_bin), "--model", str(model), "--served-model-name", "cosyvoice-3",
        "--backend", "Vulkan0", "--host", "127.0.0.1", "--port", str(args.port),
        "--concurrency", "1", "--max-llm-len", "4096", "--llm-kv-cache-type", "f16",
        "--llm-flash-attn", "0", "--flow-flash-attn", "0",
    ]
    bin_dir = app_root / "runtime" / "bin"
    env_paths = [
        bin_dir / "ollama", bin_dir / "cosyvoice", bin_dir / "ffmpeg", bin_dir,
        app_root / "runtime" / "python" / "Lib" / "site-packages" / "torch" / "lib",
    ]
    os.environ["PATH"] = os.pathsep.join(str(path) for path in env_paths) + os.pathsep + os.environ.get("PATH", "")
    os.environ["WINDOWS_SINGLE_MACHINE"] = "1"
    os.environ["AI_LIVE_STUDIO_GPU_LOCK"] = str(data_runtime / "gpu-owner.json")

    engine = ManagedEngine(
        engine_url,
        command=engine_command,
        log_path=log_path,
        idle_seconds=0,
        startup_timeout=180,
        gpu_lease=FileGpuLease(data_runtime / "gpu-owner.json", "cosyvoice3-benchmark", "CosyVoice3", "BENCHMARK"),
    )
    monitor = None
    run_id = output_dir.name
    status_path = output_dir / "status.json"
    csv_path = output_dir / "benchmark.csv"
    stop_path = output_dir / "STOP"
    nvidia_smi = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    with socket.socket() as port_probe:
        if port_probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError(f"benchmark engine port {args.port} is already in use")

    errors: list[str] = []
    rtf_values: list[float] = []
    generation_values: list[float] = []
    audio_values: list[float] = []
    actual_inferences = 0
    load_seconds = 0.0
    started_at = _now()
    state = "failed"
    cache_before = _gateway_cache_stats()
    header = [
        "timestamp", "iteration", "generation_seconds", "audio_seconds", "rtf",
        "gpu_utilization", "gpu_memory_used_mb", "gpu_memory_peak_mb", "gpu_temperature_c",
        "gpu_temperature_peak_c", "system_memory_used_mb", "system_memory_peak_mb",
        "process_memory_mb", "process_memory_peak_mb", "status", "error", "cache_hit", "cache_mode",
    ]
    try:
        monitor = _Telemetry(lambda: getattr(getattr(engine, "_process", None), "pid", None))
        monitor.start(nvidia_smi)
        baseline = dict(monitor.latest)
        _write_status(status_path, "loading", run_id, args.iterations, 0, 0, 0, None)
        load_started = time.monotonic()
        engine.ensure_ready()
        load_seconds = time.monotonic() - load_started
        voices.sync()

        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=header)
            writer.writeheader()
            csv_file.flush()
            state = "running"
            _write_status(status_path, state, run_id, args.iterations, 0, 0, load_seconds, None)
            for iteration in range(1, args.iterations + 1):
                if stop_path.exists():
                    state = "stopped"
                    break
                if monitor.unsafe_reason:
                    raise RuntimeError(monitor.unsafe_reason)
                text = _benchmark_text(iteration)
                monitor.begin_iteration(iteration)
                started = time.monotonic()
                try:
                    def synthesize(restarted: bool) -> bytes:
                        if restarted:
                            voices.invalidate_registration()
                        voices.sync()
                        return _engine_request_text(text, "default", engine_url)

                    audio = engine.run(synthesize)
                    generation_seconds = time.monotonic() - started
                    audio_seconds = _wav_seconds(audio)
                    del audio
                    actual_inferences += 1
                    rtf = generation_seconds / audio_seconds
                    generation_values.append(generation_seconds)
                    audio_values.append(audio_seconds)
                    rtf_values.append(rtf)
                    status = "ok"
                    error = ""
                except Exception as exc:
                    generation_seconds = time.monotonic() - started
                    audio_seconds = 0.0
                    rtf = 0.0
                    status = "error"
                    error = f"{type(exc).__name__}: {exc}"
                    errors.append(error)

                metrics = monitor.end_iteration()
                writer.writerow({
                    "timestamp": _now(), "iteration": iteration,
                    "generation_seconds": round(generation_seconds, 4),
                    "audio_seconds": round(audio_seconds, 4), "rtf": round(rtf, 4),
                    "gpu_utilization": metrics.get("gpu_utilization", ""),
                    "gpu_memory_used_mb": metrics.get("gpu_memory_used_mb", ""),
                    "gpu_memory_peak_mb": metrics.get("gpu_memory_peak_mb", ""),
                    "gpu_temperature_c": metrics.get("gpu_temperature_c", ""),
                    "gpu_temperature_peak_c": metrics.get("gpu_temperature_peak_c", ""),
                    "system_memory_used_mb": metrics.get("system_memory_used_mb", ""),
                    "system_memory_peak_mb": metrics.get("system_memory_peak_mb", ""),
                    "process_memory_mb": metrics.get("process_memory_mb", ""),
                    "process_memory_peak_mb": metrics.get("process_memory_peak_mb", ""),
                    "status": status, "error": error, "cache_hit": 0,
                    "cache_mode": "direct_engine_bypass",
                })
                csv_file.flush()
                os.fsync(csv_file.fileno())
                _write_status(status_path, "running" if status == "ok" else "failed", run_id,
                              args.iterations, iteration if status == "ok" else iteration - 1,
                              actual_inferences, load_seconds, rtf if status == "ok" else None, error)
                if status != "ok" or monitor.unsafe_reason:
                    if monitor.unsafe_reason:
                        errors.append(monitor.unsafe_reason)
                    state = "failed"
                    break
                if iteration % 10 == 0:
                    print(f"completed={iteration}/{args.iterations} rtf={rtf:.3f} gpu={metrics.get('gpu_memory_used_mb')} MiB", flush=True)
                if iteration == min(10, args.iterations):
                    monitor.enable_growth_guard()

        if not errors and actual_inferences == args.iterations and not stop_path.exists():
            state = "pass"
        elif stop_path.exists() and not errors:
            state = "stopped"
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        state = "failed"
    finally:
        before_close = dict(monitor.latest) if monitor and monitor.latest else {}
        try:
            engine.close()
        except Exception as exc:
            errors.append(f"engine close failed: {exc}")
            state = "failed"
        if engine.stats().get("state") == "stop_failed":
            errors.append(str(engine.stats().get("last_error") or "CosyVoice process did not stop cleanly"))
            state = "failed"
        if monitor:
            time.sleep(2.0)
            monitor.close()

    samples = monitor.samples if monitor else []
    gpu_peak = max((item["gpu_memory_used_mb"] for item in samples), default=0)
    gpu_temp_peak = max((item["gpu_temperature_c"] for item in samples), default=0)
    ram_peak = max((item["system_memory_used_mb"] for item in samples), default=0)
    process_peak = max((item["process_memory_mb"] for item in samples), default=0)
    final_sample = samples[-1] if samples else {}
    try:
        engine_log_count = sum(1 for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                               if "TTS generated:" in line)
    except OSError:
        engine_log_count = 0
    if actual_inferences != engine_log_count:
        errors.append(f"inference proof mismatch: successful WAVs={actual_inferences}, engine TTS log lines={engine_log_count}")
        state = "failed"
    cache_after = _gateway_cache_stats()

    report = {
        "task_id": "cosyvoice3-windows-no-cache-benchmark-002",
        "run_id": run_id,
        "result": "PASS" if state == "pass" and not errors else "PARTIAL",
        "gpu": "NVIDIA GeForce RTX 3060",
        "gpu_total_mb": baseline.get("gpu_memory_total_mb", 12288),
        "ram_total_mb": samples[0]["system_memory_used_mb"] + samples[0]["system_memory_available_mb"] if samples else 0,
        "cosyvoice_backend": "Vulkan0",
        "voice": "default -> speaker_c_reviewed.gguf",
        "iterations_requested": args.iterations,
        "successful_generations": actual_inferences,
        "engine_log_inference_count": engine_log_count,
        "cache_hit": 0,
        "cache_miss": "not applicable: direct engine endpoint bypassed TTSResultCache",
        "gateway_cache_before": cache_before,
        "gateway_cache_after": cache_after,
        "model_load_seconds": round(load_seconds, 3),
        "total_seconds": round(sum(generation_values) + load_seconds, 3),
        "average_generation_seconds": round(sum(generation_values) / len(generation_values), 3) if generation_values else None,
        "average_audio_seconds": round(sum(audio_values) / len(audio_values), 3) if audio_values else None,
        "average_rtf": round(sum(rtf_values) / len(rtf_values), 4) if rtf_values else None,
        "p50_rtf": round(_percentile(rtf_values, 0.50), 4) if rtf_values else None,
        "p95_rtf": round(_p95(rtf_values), 4) if rtf_values else None,
        "max_rtf": round(max(rtf_values), 4) if rtf_values else None,
        "gpu_memory_start_mb": baseline.get("gpu_memory_used_mb"),
        "gpu_memory_peak_mb": gpu_peak,
        "gpu_memory_end_loaded_mb": before_close.get("gpu_memory_used_mb"),
        "gpu_memory_end_released_mb": final_sample.get("gpu_memory_used_mb"),
        "gpu_temperature_peak_c": gpu_temp_peak,
        "system_memory_peak_mb": ram_peak,
        "system_memory_end_used_mb": final_sample.get("system_memory_used_mb"),
        "cosyvoice_process_memory_peak_mb": process_peak,
        "errors": errors,
        "started_at": started_at,
        "finished_at": _now(),
        "csv_path": str(csv_path),
        "engine_log_path": str(log_path),
        "status_path": str(status_path),
    }
    _write_json(output_dir / "report.json", report)
    lines = [
        f"RESULT: {report['result']}",
        f"Task ID: {report['task_id']}",
        f"GPU / backend: {report['gpu']} / {report['cosyvoice_backend']}",
        f"Voice: {report['voice']}",
        f"Iterations: {report['successful_generations']}/{report['iterations_requested']}",
        "Cache: hit=0; bypassed TTSResultCache through direct engine /tts endpoint",
        f"Model load: {report['model_load_seconds']} s",
        f"Peak VRAM: {report['gpu_memory_peak_mb']} MiB; end after release: {report['gpu_memory_end_released_mb']} MiB",
        f"Peak system RAM: {report['system_memory_peak_mb']} MiB",
        f"RTF avg/P50/P95/max: {report['average_rtf']} / {report['p50_rtf']} / {report['p95_rtf']} / {report['max_rtf']}",
        f"Engine log inference lines: {engine_log_count}",
        f"CSV: {csv_path}",
        f"Errors: {json.dumps(errors, ensure_ascii=False)}",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_status(status_path, state, run_id, args.iterations, actual_inferences,
                  actual_inferences, load_seconds, rtf_values[-1] if rtf_values else None,
                  "; ".join(errors))
    print("\n".join(lines), flush=True)
    return 0 if report["result"] == "PASS" else 1


def self_test() -> None:
    audio = io.BytesIO()
    with wave.open(audio, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\0\0" * 2400)
    assert abs(_wav_seconds(audio.getvalue()) - 0.1) < 0.001
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    print("benchmark self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path(r"C:\Program Files\AI Live Studio"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", "")) / "AI-Live-Studio")
    parser.add_argument("--output-dir", type=Path, required=False)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not 1 <= args.iterations <= 1000:
        parser.error("iterations must be between 1 and 1000")
    if args.output_dir is None:
        parser.error("--output-dir is required")
    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
