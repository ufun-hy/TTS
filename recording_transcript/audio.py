"""Shared local audio decoding for Qwen ASR workflows."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4"}


class AudioDecodeError(ValueError):
    """A user-facing local audio decode error."""


def decode_audio(source: Path, workdir: Path) -> Path:
    """Decode a supported source into 16 kHz mono PCM WAV with ffmpeg."""
    source = source.expanduser()
    if not source.is_file():
        raise AudioDecodeError("上传的录音文件不存在")
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise AudioDecodeError("仅支持 wav、mp3、m4a、mp4 录音")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioDecodeError("ffmpeg 未安装，无法解码录音")

    workdir.mkdir(parents=True, exist_ok=True)
    output = workdir / "audio.wav"
    completed = subprocess.run(
        [
            ffmpeg, "-v", "error", "-nostdin", "-y",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown ffmpeg error"
        raise AudioDecodeError(f"录音解码失败：{message}")
    return output
