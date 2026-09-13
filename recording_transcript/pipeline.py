"""Local Qwen3-ASR-1.7B MLX pipeline for the standalone transcript page."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
from typing import Any, Callable

from .cleaner import clean_transcript
from .qwen_asr import transcribe as transcribe_qwen, validate_model


class TranscriptError(RuntimeError):
    """A user-facing recording transcription error."""


def _audio_ingest_module(project_root: Path) -> Any:
    path = project_root / "scripts" / "audio-ingest.py"
    spec = importlib.util.spec_from_file_location("tts_audio_ingest", path)
    if spec is None or spec.loader is None:
        raise TranscriptError("无法加载现有音频解码模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _segment_texts(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise TranscriptError("本地 ASR 返回了无效结果")
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        raise TranscriptError("本地 ASR 没有返回可用片段")
    segments: list[dict[str, Any]] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            segments.append({
                "text": text,
                "start": segment.get("start", 0),
                "end": segment.get("end", 0),
            })
    if not segments:
        raise TranscriptError("录音中没有识别到可用语音")
    return segments


def transcribe_recording(
    source: Path,
    project_root: Path,
    model: Path,
    on_stage: Callable[[str], None] | None = None,
) -> str:
    """Decode input and run only the local Qwen3-ASR-1.7B MLX model."""
    source = source.expanduser()
    if not source.is_file():
        raise TranscriptError("上传的录音文件不存在")
    if source.suffix.lower() not in {".wav", ".mp3", ".m4a", ".mp4"}:
        raise TranscriptError("仅支持 wav、mp3、m4a、mp4 录音")
    try:
        model = validate_model(model)
    except ValueError as exc:
        raise TranscriptError(str(exc)) from exc

    ingest = _audio_ingest_module(project_root)
    try:
        if on_stage:
            on_stage("recognizing")
        with tempfile.TemporaryDirectory(prefix="recording-transcript-") as workdir:
            audio = ingest._audio_for_asr(source, Path(workdir))
            result = transcribe_qwen(audio, model)
    except ImportError as exc:
        raise TranscriptError("当前 Python 环境缺少 Qwen MLX 依赖，请使用 Qwen ASR 环境启动") from exc
    except ingest.IngestError as exc:
        raise TranscriptError(str(exc)) from exc
    except OSError as exc:
        raise TranscriptError(f"录音处理失败：{exc}") from exc
    except Exception as exc:
        raise TranscriptError(f"本地 ASR 处理失败：{exc}") from exc

    segments = _segment_texts(result)
    if on_stage:
        on_stage("cleaning")
    final_text = clean_transcript("".join(item["text"] for item in segments), segments)
    if not final_text:
        raise TranscriptError("录音中没有生成可阅读文稿")
    return final_text
