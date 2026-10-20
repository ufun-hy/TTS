"""Compatibility interface for the Windows Qwen3-ASR CUDA backend.

The standalone Windows build uses the isolated CUDA worker only. MLX,
Apple-specific fallback and environment-based backend switching are removed.
"""
from __future__ import annotations

from .qwen_asr_windows import (
    BACKEND,
    MODEL_DIRECTORY,
    WorkerLifecycleError,
    readiness,
    transcribe,
    validate_model,
)

__all__ = [
    "BACKEND", "MODEL_DIRECTORY", "WorkerLifecycleError",
    "readiness", "transcribe", "validate_model",
]
