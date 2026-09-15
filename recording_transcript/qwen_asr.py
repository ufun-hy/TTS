"""Local Qwen3-ASR-1.7B MLX backend with no backend selection or fallback."""
from __future__ import annotations

from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import platform
import os
from typing import Any

MODEL_DIRECTORY = 'qwen3-asr-1.7b-bf16'
BACKEND = 'qwen3-asr-1.7b-mlx'
CHUNK_SECONDS = 30.0
MAX_TOKENS = 1024


def validate_model(model: Path) -> Path:
    if platform.system() == "Windows" or os.environ.get("RECORDING_TRANSCRIPT_BACKEND") == "cuda":
        from .qwen_asr_windows import validate_model as validate_windows
        return validate_windows(model)
    model = model.expanduser().resolve()
    if not model.is_dir():
        raise ValueError('本地 Qwen3-ASR-1.7B MLX 模型未找到，请配置 RECORDING_TRANSCRIPT_MODEL')
    try:
        config = json.loads((model / 'config.json').read_text(encoding='utf-8'))
        text_config = config['thinker_config']['text_config']
        correct = (config['model_type'] == 'qwen3_asr'
                   and text_config['hidden_size'] == 2048
                   and text_config['num_hidden_layers'] == 28)
    except (OSError, ValueError, KeyError, TypeError):
        correct = False
    if not correct:
        raise ValueError('仅支持 Qwen3-ASR-1.7B MLX 权重')
    required = ('tokenizer_config.json', 'preprocessor_config.json', 'vocab.json', 'merges.txt')
    if any(not (model / name).is_file() for name in required) or not any(model.glob('*.safetensors')):
        raise ValueError('Qwen3-ASR-1.7B 本地模型文件不完整，请完成安装；服务不会自动下载')
    return model


def readiness(model: Path) -> dict[str, Any]:
    if platform.system() == "Windows" or os.environ.get("RECORDING_TRANSCRIPT_BACKEND") == "cuda":
        from .qwen_asr_windows import readiness as readiness_windows
        return readiness_windows(model)
    try:
        validate_model(model)
        if platform.system() != 'Darwin' or platform.machine() != 'arm64':
            raise ValueError('Qwen3-ASR MLX 需要 Apple Silicon Mac')
        if importlib.util.find_spec('mlx_audio') is None:
            raise ValueError('请使用 Qwen ASR Python 环境启动，当前环境缺少 mlx-audio')
    except ValueError as exc:
        return {'asr_ready': False, 'asr_error': str(exc), 'asr_backend': BACKEND}
    return {'asr_ready': True, 'asr_error': '', 'asr_backend': BACKEND}


@lru_cache(maxsize=1)
def _load_model(model: Path):
    from mlx_audio.stt import load
    return load(str(model))


def transcribe(audio: Path, model: Path) -> dict[str, Any]:
    if platform.system() == "Windows" or os.environ.get("RECORDING_TRANSCRIPT_BACKEND") == "cuda":
        from .qwen_asr_windows import transcribe as transcribe_windows
        return transcribe_windows(audio, model)
    """Caller serializes GPU jobs; cache only the currently configured model."""
    model = validate_model(model)
    status = readiness(model)
    if not status['asr_ready']:
        raise ValueError(status['asr_error'])
    import mlx.core as mx
    if mx.default_device() != mx.gpu:
        raise RuntimeError('Qwen3-ASR MLX 未使用 Apple GPU，停止转录')
    result = _load_model(model).generate(
        str(audio), language='Chinese', temperature=0.0,
        chunk_duration=CHUNK_SECONDS, max_tokens=MAX_TOKENS,
    )
    if not isinstance(result.text, str) or not result.text.strip():
        raise ValueError('Qwen3-ASR 没有识别到可用语音')
    return {'text': result.text, 'segments': result.segments}
