"""Lightweight silence-aware WAV chunking for the Windows ASR worker."""

from __future__ import annotations

import audioop
from dataclasses import dataclass
from pathlib import Path
import wave


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start: float
    end: float


def split_wav(source: Path, output_dir: Path, target_seconds: float = 30.0, max_seconds: float = 60.0) -> list[AudioChunk]:
    if target_seconds <= 0 or max_seconds < target_seconds:
        raise ValueError("max_seconds must be at least target_seconds")
    output_dir.mkdir(parents=True, exist_ok=True)
    with wave.open(str(source), "rb") as audio:
        channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
        if channels != 1 or width not in (2, 4) or rate <= 0:
            raise ValueError("ASR input must be mono 16-bit or 32-bit PCM WAV")
        raw = audio.readframes(frames)
    if not raw:
        raise ValueError("ASR input WAV is empty")
    frame_size = max(1, int(rate * 0.1))
    silence_frames = max(1, int(rate * 0.35))
    threshold = 350 if width == 2 else 350 * 65536
    cuts: list[int] = []
    silence_start: int | None = None
    for start in range(0, frames, frame_size):
        count = min(frame_size, frames - start)
        sample = raw[start * width:(start + count) * width]
        quiet = audioop.rms(sample, width) <= threshold
        if quiet and silence_start is None:
            silence_start = start
        elif not quiet and silence_start is not None:
            if start - silence_start >= silence_frames:
                cuts.append(start)
            silence_start = None

    boundaries: list[int] = [0]
    cursor = 0
    target = int(rate * target_seconds)
    maximum = int(rate * max_seconds)
    silence_cuts = sorted(set(cuts))
    while cursor < frames:
        maximum_end = min(frames, cursor + maximum)
        if maximum_end == frames:
            cut = frames
        else:
            candidates = [value for value in silence_cuts if cursor + target <= value <= maximum_end]
            cut = min(candidates, key=lambda value: abs(value - (cursor + target))) if candidates else maximum_end
        if cut <= cursor:
            cut = min(frames, cursor + maximum)
        boundaries.append(cut)
        cursor = cut

    chunks: list[AudioChunk] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        if end <= start:
            continue
        path = output_dir / f"chunk-{index:04d}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(width)
            output.setframerate(rate)
            output.writeframes(raw[start * width:end * width])
        chunks.append(AudioChunk(path, start / rate, end / rate))
    if not chunks:
        raise ValueError("ASR input produced no audio chunks")
    return chunks
