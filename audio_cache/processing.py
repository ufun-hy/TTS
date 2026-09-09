"""Optional WAV speed and volume processing for cached audio."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from timeline.cache import wav_duration


class AudioProcessingError(RuntimeError):
    """Audio could not be processed without risking a bad cache artifact."""


@dataclass
class AudioProcessingConfig:
    speed_enabled: bool = True
    speed_min: float = 0.92
    speed_max: float = 1.15
    volume_enabled: bool = True
    volume_target_db: float = -16.0
    volume_max_gain: float = 3.0

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "AudioProcessingConfig":
        raw = raw or {}
        speed = raw.get("speed", {}) if isinstance(raw.get("speed", {}), dict) else {}
        volume = raw.get("volume", {}) if isinstance(raw.get("volume", {}), dict) else {}
        config = cls(
            speed_enabled=bool(speed.get("enabled", cls.speed_enabled)),
            speed_min=float(speed.get("min", cls.speed_min)),
            speed_max=float(speed.get("max", cls.speed_max)),
            volume_enabled=bool(volume.get("enabled", cls.volume_enabled)),
            volume_target_db=float(volume.get("target_db", cls.volume_target_db)),
            volume_max_gain=float(volume.get("max_gain", cls.volume_max_gain)),
        )
        if not 0 < config.speed_min <= config.speed_max:
            raise ValueError("speed min/max must be positive and ordered")
        if config.volume_max_gain < 0:
            raise ValueError("volume max_gain must be non-negative")
        return config


@dataclass
class ProcessingResult:
    audio: bytes
    raw_duration: float
    duration: float
    speed_factor: float = 1.0
    volume_gain: float = 0.0
    warnings: List[str] = field(default_factory=list)


def calculate_speed_factor(raw_duration: float, target_duration: Optional[float], config: AudioProcessingConfig) -> tuple[float, List[str]]:
    if not config.speed_enabled or not target_duration or target_duration <= 0 or raw_duration <= 0:
        return 1.0, []
    requested = raw_duration / target_duration
    factor = min(config.speed_max, max(config.speed_min, requested))
    warnings = []
    if abs(requested - factor) > 1e-9:
        warnings.append("speed_factor_out_of_range")
    return factor, warnings


def calculate_volume_gain(measured_db: float, config: AudioProcessingConfig) -> tuple[float, List[str]]:
    if not config.volume_enabled:
        return 0.0, []
    requested = config.volume_target_db - measured_db
    gain = min(config.volume_max_gain, max(-config.volume_max_gain, requested))
    warnings = []
    if abs(requested - gain) > 1e-9:
        warnings.append("volume_gain_out_of_range")
    return gain, warnings


class AudioProcessor:
    def __init__(self, config: Optional[AudioProcessingConfig] = None, ffmpeg: Optional[str] = None) -> None:
        self.config = config or AudioProcessingConfig()
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")

    def process(self, audio: bytes, metadata: Dict[str, Any]) -> ProcessingResult:
        with tempfile.TemporaryDirectory(prefix="audio-cache-") as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(audio)
            raw_duration = wav_duration(source)
            target = _optional_float(metadata.get("target_duration"))
            speed_factor, warnings = calculate_speed_factor(raw_duration, target, self.config)

            filters: List[str] = []
            if abs(speed_factor - 1.0) > 1e-9:
                filters.append(f"atempo={speed_factor:.8f}")

            measured_db: Optional[float] = None
            if self.config.volume_enabled:
                measured_db = self._measure_volume(source)
                gain, volume_warnings = calculate_volume_gain(measured_db, self.config)
                warnings.extend(volume_warnings)
                if abs(gain) > 1e-9:
                    filters.append(f"volume={gain:.8f}dB")
            else:
                gain = 0.0

            if not filters:
                processed = audio
            else:
                if not self.ffmpeg:
                    raise AudioProcessingError("ffmpeg is required for enabled audio processing")
                output = root / "processed.wav"
                self._run_ffmpeg(source, output, ",".join(filters))
                processed = output.read_bytes()

            final_path = root / "final.wav"
            final_path.write_bytes(processed)
            duration = wav_duration(final_path)
            return ProcessingResult(processed, raw_duration, duration, speed_factor, gain, warnings)

    def _measure_volume(self, source: Path) -> float:
        if not self.ffmpeg:
            raise AudioProcessingError("ffmpeg is required for enabled volume processing")
        completed = subprocess.run(
            [self.ffmpeg, "-v", "info", "-nostdin", "-i", str(source), "-af", "volumedetect", "-f", "null", "-"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise AudioProcessingError(completed.stderr.strip() or "ffmpeg volume detection failed")
        if re.search(r"mean_volume:\s*-inf\s*dB", completed.stderr):
            # Silence has no useful loudness target; leave it unchanged.
            return self.config.volume_target_db
        match = re.search(r"mean_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB", completed.stderr)
        if not match:
            raise AudioProcessingError("ffmpeg did not report mean_volume")
        return float(match.group(1))

    def _run_ffmpeg(self, source: Path, output: Path, filters: str) -> None:
        completed = subprocess.run(
            [
                self.ffmpeg or "ffmpeg",
                "-v", "error",
                "-nostdin",
                "-y",
                "-i", str(source),
                "-filter:a", filters,
                "-c:a", "pcm_s16le",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode or not output.is_file():
            raise AudioProcessingError(completed.stderr.strip() or "ffmpeg audio processing failed")


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AudioProcessingError(f"invalid target_duration: {value!r}") from exc
