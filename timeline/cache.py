"""Content-addressed WAV cache for Timeline Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import wave
from typing import Any, Dict, Optional


@dataclass
class CachedAudio:
    path: Path
    metadata: Dict[str, Any]
    cache_hit: bool = True


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate()
            return audio.getnframes() / rate if rate else 0.0
    except (wave.Error, OSError):
        # CosyVoice returns IEEE-float WAV (format 3), which Python 3.9's
        # wave module rejects even though afplay can play it.
        data = path.read_bytes()
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError(f"unsupported WAV: {path}")
        offset = 12
        byte_rate = 0
        data_size = 0
        while offset + 8 <= len(data):
            chunk, size = struct.unpack_from("<4sI", data, offset)
            body = offset + 8
            if chunk == b"fmt " and size >= 16:
                byte_rate = struct.unpack_from("<I", data, body + 8)[0]
            elif chunk == b"data":
                data_size = size
                break
            offset = body + size + (size & 1)
        return data_size / byte_rate if byte_rate else 0.0


class AudioCache:
    def __init__(self, root: Path, timeline_id: str, session_id: str) -> None:
        self.timeline_root = root / _safe(timeline_id)
        self.session_root = self.timeline_root / _safe(session_id)
        self.session_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(text: str, voice: str, params: Optional[Dict[str, Any]] = None) -> str:
        payload = json.dumps({"text": text, "voice": voice, "params": params or {}}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def lookup(self, segment_id: str, text: str, voice: str, params: Optional[Dict[str, Any]] = None) -> Optional[CachedAudio]:
        cache_key = self.key(text, voice, params)
        metadata_path = self.session_root / f"{_safe(segment_id)}-{cache_key[:16]}.json"
        metadata = self._read(metadata_path)
        if metadata:
            path = Path(metadata["file"])
            if path.is_file() and metadata.get("cache_key") == cache_key:
                return CachedAudio(path, metadata)
        for candidate in self.timeline_root.glob(f"*/{_safe(segment_id)}-{cache_key[:16]}.json"):
            metadata = self._read(candidate)
            if metadata:
                path = Path(metadata["file"])
                if path.is_file() and metadata.get("cache_key") == cache_key:
                    return CachedAudio(path, metadata)
        return None

    def store(self, segment_id: str, text: str, voice: str, audio: bytes, params: Optional[Dict[str, Any]] = None) -> CachedAudio:
        cache_key = self.key(text, voice, params)
        stem = f"{_safe(segment_id)}-{cache_key[:16]}"
        audio_path = self.session_root / f"{stem}.wav"
        metadata_path = self.session_root / f"{stem}.json"
        with tempfile.NamedTemporaryFile(dir=self.session_root, suffix=".wav", delete=False) as temporary:
            temporary.write(audio)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, audio_path)
        duration = wav_duration(audio_path)
        metadata = {
            "segment_id": segment_id,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "cache_key": cache_key,
            "voice": voice,
            "file": str(audio_path),
            "audio_duration": duration,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        with metadata_path.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return CachedAudio(audio_path, metadata, cache_hit=False)

    @staticmethod
    def _read(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, KeyError):
            return None
