"""Qwen3-ASR Transformers/CUDA backend used on Windows."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import wave
from functools import lru_cache

from .windows_chunks import split_wav

MODEL_DIRECTORY = "Qwen3-ASR-1.7B"
BACKEND = "qwen3-asr-1.7b-cuda"


class WorkerLifecycleError(RuntimeError):
    """The CUDA worker could not be confirmed dead after an abnormal exit."""

    worker_exited = False


def validate_model(model: Path) -> Path:
    model = model.expanduser().resolve()
    if not model.is_dir():
        raise ValueError("Windows Qwen3-ASR-1.7B 模型未找到，请配置 RECORDING_TRANSCRIPT_MODEL")
    try:
        config = json.loads((model / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Qwen3-ASR Windows 权重缺少有效 config.json") from exc
    if config.get("model_type") != "qwen3_asr":
        raise ValueError("仅支持 Qwen3-ASR-1.7B Transformers 权重")
    if not any(model.glob("*.safetensors")):
        raise ValueError("Qwen3-ASR Windows 权重不完整：缺少 safetensors 文件")
    return model


def readiness(model: Path) -> dict[str, Any]:
    try:
        validate_model(model)
        if importlib.util.find_spec("torch") is None or importlib.util.find_spec("qwen_asr") is None:
            raise ValueError("Windows ASR 环境缺少 torch 或 qwen-asr")
        import torch
        if not torch.cuda.is_available():
            raise ValueError("未检测到可用 CUDA GPU")
    except (ValueError, ImportError) as exc:
        return {"asr_ready": False, "asr_error": str(exc), "asr_backend": BACKEND}
    return {"asr_ready": True, "asr_error": "", "asr_backend": BACKEND}


@lru_cache(maxsize=1)
def _load_model(model: Path):
    import torch
    from qwen_asr import Qwen3ASRModel
    return Qwen3ASRModel.from_pretrained(
        str(model), dtype=torch.bfloat16, device_map="cuda:0",
        max_inference_batch_size=1, max_new_tokens=1024,
    )


def _transcribe_one(audio: Path, model: Path) -> dict[str, Any]:
    recognizer = _load_model(model)
    results = recognizer.transcribe(audio=str(audio), language="Chinese")
    if not isinstance(results, list) or not results or not isinstance(getattr(results[0], "text", None), str):
        raise ValueError("Qwen3-ASR 没有返回有效文本")
    text = results[0].text.strip()
    if not text:
        raise ValueError("Qwen3-ASR 没有识别到可用语音")
    segments = getattr(results[0], "segments", None)
    if not isinstance(segments, list) or not segments:
        with wave.open(str(audio), "rb") as handle:
            duration = handle.getnframes() / max(1, handle.getframerate())
        segments = [{"text": text, "start": 0.0, "end": duration}]
    return {"text": text, "segments": segments}


def _transcribe_in_process(audio: Path, model: Path) -> dict[str, Any]:
    """Chunk a decoded WAV, run one CUDA model process, and restore offsets."""
    model = validate_model(model)
    status = readiness(model)
    if not status["asr_ready"]:
        raise ValueError(status["asr_error"])
    chunks = split_wav(audio, audio.parent / "asr-chunks")
    all_segments: list[dict[str, Any]] = []
    texts: list[str] = []
    for chunk in chunks:
        result = _transcribe_one(chunk.path, model)
        texts.append(result["text"])
        for segment in result["segments"]:
            if not isinstance(segment, dict):
                continue
            try:
                start = float(segment.get("start", 0)) + chunk.start
                end = float(segment.get("end", 0)) + chunk.start
            except (TypeError, ValueError):
                start, end = chunk.start, chunk.end
            all_segments.append({"text": str(segment.get("text") or "").strip(), "start": start, "end": end})
    return {"text": "".join(texts), "segments": [item for item in all_segments if item["text"]]}


def transcribe(audio: Path, model: Path) -> dict[str, Any]:
    """Run the CUDA worker out-of-process so its model is released on exit."""
    if os.environ.get("QWEN_ASR_WORKER") == "1":
        return _transcribe_in_process(audio, model)
    env = os.environ.copy()
    env["QWEN_ASR_WORKER"] = "1"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "recording_transcript.windows_worker", str(audio), str(model)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout, stderr = process.communicate(timeout=3600)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            try:
                process.kill()
                process.communicate(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                raise WorkerLifecycleError("Windows ASR worker remained alive after timeout") from exc
            if process.poll() is None:
                raise WorkerLifecycleError("Windows ASR worker remained alive after timeout") from exc
        raise RuntimeError("Windows ASR worker 超时") from exc
    if process.poll() is None:
        try:
            process.kill()
            process.communicate(timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerLifecycleError("Windows ASR worker 尚未退出") from exc
        if process.poll() is None:
            raise WorkerLifecycleError("Windows ASR worker 尚未退出")
    if process.returncode:
        raise RuntimeError((stderr or stdout or "Windows ASR worker failed").strip()[-2000:])
    try:
        value = json.loads(stdout)
    except ValueError as exc:
        raise RuntimeError("Windows ASR worker 返回了无效 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("segments"), list):
        raise RuntimeError("Windows ASR worker 返回了不完整结果")
    return value
