"""Prepare cached WAV files for Windows MCI waveaudio playback."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import struct
import tempfile
import wave


CONVERSION_VERSION = 1
_PCM_GUID = bytes.fromhex("01000000 0000 1000 8000 00aa00389b71")
_FLOAT_GUID = bytes.fromhex("03000000 0000 1000 8000 00aa00389b71")
_CHUNK_FRAMES = 16_384


class WavCompatibilityError(RuntimeError):
    """The source cannot be safely converted for MCI playback."""


@dataclass(frozen=True)
class WavInfo:
    format_code: int
    channels: int
    sample_rate: int
    bits_per_sample: int
    block_align: int
    data_offset: int
    data_size: int
    frames: int
    sample_kind: str


def prepare_mci_wav(source: Path) -> Path:
    """Return a validated PCM16 path suitable for Windows MCI.

    PCM16 sources are returned unchanged. Float32 sources are converted into
    ``<source>/pcm16`` with an atomic, versioned cache file.
    """
    source = Path(source)
    info = _inspect_wav(source)
    if info.sample_kind == "pcm16":
        return source
    if info.sample_kind != "float32":
        raise WavCompatibilityError(f"unsupported WAV encoding: {info.sample_kind}")

    signature = _source_signature(source)
    cache_dir = source.parent / "pcm16"
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = _conversion_cache_path(source, signature)
    if destination.is_file():
        try:
            cached = _inspect_wav(destination)
            if _matches_pcm16(cached, info):
                return destination
        except (OSError, WavCompatibilityError):
            pass

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_dir,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        _convert_float32(source, info, signature, temporary)
        converted = _inspect_wav(temporary)
        if not _matches_pcm16(converted, info):
            raise WavCompatibilityError("converted WAV metadata does not match source")
        if _source_signature(source) != signature:
            raise WavCompatibilityError("source WAV changed during conversion")
        temporary.replace(destination)
        if _source_signature(source) != signature:
            destination.unlink(missing_ok=True)
            raise WavCompatibilityError("source WAV changed after conversion")
        return destination
    except WavCompatibilityError:
        raise
    except (OSError, struct.error, wave.Error) as exc:
        raise WavCompatibilityError(f"WAV conversion failed: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def is_float32_wav(source: Path) -> bool:
    """Return true only for a structurally valid IEEE Float32 WAV."""
    try:
        return _inspect_wav(Path(source)).sample_kind == "float32"
    except (OSError, WavCompatibilityError):
        return False


def inspect_wav(source: Path) -> WavInfo:
    """Return validated structural metadata for a WAV file."""
    return _inspect_wav(Path(source))


def conversion_cache_path(source: Path) -> Path:
    """Return the versioned derived-file path for the current source version."""
    source = Path(source)
    return _conversion_cache_path(source, _source_signature(source))


def conversion_cache_valid(source: Path) -> bool:
    """Return whether the current source version already has a valid PCM16 derivative."""
    source = Path(source)
    try:
        source_info = _inspect_wav(source)
        candidate = conversion_cache_path(source)
        return candidate.is_file() and _matches_pcm16(_inspect_wav(candidate), source_info)
    except (OSError, WavCompatibilityError):
        return False


def _source_signature(source: Path) -> tuple[int, int]:
    stat = source.stat()
    return stat.st_size, stat.st_mtime_ns


def _conversion_cache_path(source: Path, signature: tuple[int, int]) -> Path:
    return source.parent / "pcm16" / (
        f"{source.name}.pcm16-v{CONVERSION_VERSION}-{signature[0]}-{signature[1]}.wav"
    )


def _inspect_wav(path: Path) -> WavInfo:
    try:
        file_size = path.stat().st_size
        if file_size < 12:
            raise WavCompatibilityError("WAV is truncated before RIFF header")
        with path.open("rb") as handle:
            header = handle.read(12)
            if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise WavCompatibilityError("invalid RIFF/WAVE container")
            riff_size = struct.unpack_from("<I", header, 4)[0]
            riff_end = 8 + riff_size
            if riff_end > file_size or riff_end < 12:
                raise WavCompatibilityError("truncated RIFF data")

            fmt: tuple[int, int, int, int, int, int, bytes | None] | None = None
            data_offset = data_size = 0
            offset = 12
            while offset < riff_end:
                if offset + 8 > riff_end:
                    raise WavCompatibilityError("truncated WAV chunk header")
                handle.seek(offset)
                chunk_header = handle.read(8)
                if len(chunk_header) != 8:
                    raise WavCompatibilityError("truncated WAV chunk header")
                chunk_id = chunk_header[:4]
                chunk_size = struct.unpack_from("<I", chunk_header, 4)[0]
                start = offset + 8
                end = start + chunk_size
                padded_end = end + (chunk_size & 1)
                if end > riff_end or end > file_size or padded_end > riff_end:
                    raise WavCompatibilityError("truncated WAV chunk")
                if chunk_id == b"fmt ":
                    if chunk_size < 16 or chunk_size > 4096:
                        raise WavCompatibilityError("invalid WAV format chunk")
                    handle.seek(start)
                    raw_fmt = handle.read(chunk_size)
                    if len(raw_fmt) != chunk_size:
                        raise WavCompatibilityError("truncated WAV format chunk")
                    code, channels, rate, byte_rate, align, bits = struct.unpack_from(
                        "<HHIIHH", raw_fmt
                    )
                    subtype = None
                    if code == 0xFFFE:
                        if chunk_size < 40:
                            raise WavCompatibilityError("invalid extensible WAV format")
                        cb_size = struct.unpack_from("<H", raw_fmt, 16)[0]
                        if cb_size < 22 or 18 + cb_size > chunk_size:
                            raise WavCompatibilityError("invalid extensible WAV format size")
                        subtype = raw_fmt[24:40]
                        if subtype == _PCM_GUID:
                            code = 1
                        elif subtype == _FLOAT_GUID:
                            code = 3
                        else:
                            raise WavCompatibilityError("unsupported WAV subformat GUID")
                    fmt = code, channels, rate, byte_rate, align, bits, subtype
                elif chunk_id == b"data" and not data_size:
                    data_offset, data_size = start, chunk_size
                offset = padded_end

            if offset != riff_end:
                raise WavCompatibilityError("invalid RIFF chunk boundaries")
    except WavCompatibilityError:
        raise
    except (OSError, struct.error) as exc:
        raise WavCompatibilityError(f"invalid WAV audio: {exc}") from exc

    if fmt is None or not data_size:
        raise WavCompatibilityError("WAV must contain fmt and data chunks")
    code, channels, rate, byte_rate, align, bits, _subtype = fmt
    if channels <= 0 or rate <= 0 or bits not in (16, 32):
        raise WavCompatibilityError("unsupported WAV channel, rate or sample width")
    expected_align = channels * (bits // 8)
    if align != expected_align or byte_rate != rate * align:
        raise WavCompatibilityError("invalid WAV block alignment")
    if data_size % align:
        raise WavCompatibilityError("WAV data is not frame-aligned")
    if code == 1 and bits == 16:
        kind = "pcm16"
    elif code == 3 and bits == 32:
        kind = "float32"
    else:
        raise WavCompatibilityError("unsupported WAV encoding")
    return WavInfo(code, channels, rate, bits, align, data_offset, data_size, data_size // align, kind)


def _matches_pcm16(candidate: WavInfo, source: WavInfo) -> bool:
    return (
        candidate.sample_kind == "pcm16"
        and candidate.channels == source.channels
        and candidate.sample_rate == source.sample_rate
        and candidate.frames == source.frames
    )


def _convert_float32(source: Path, info: WavInfo, signature: tuple[int, int], destination: Path) -> None:
    with source.open("rb") as input_handle, wave.open(str(destination), "wb") as output_handle:
        output_handle.setnchannels(info.channels)
        output_handle.setsampwidth(2)
        output_handle.setframerate(info.sample_rate)
        input_handle.seek(info.data_offset)
        remaining = info.data_size
        while remaining:
            if _source_signature(source) != signature:
                raise WavCompatibilityError("source WAV changed during conversion")
            chunk_size = min(remaining, _CHUNK_FRAMES * info.block_align)
            chunk = input_handle.read(chunk_size)
            if len(chunk) != chunk_size:
                raise WavCompatibilityError("truncated WAV sample data")
            converted = bytearray((chunk_size // 4) * 2)
            output_offset = 0
            for input_offset in range(0, len(chunk), 4):
                value = struct.unpack_from("<f", chunk, input_offset)[0]
                if not math.isfinite(value):
                    raise WavCompatibilityError("WAV contains NaN or Infinity sample")
                sample = max(-32768, min(32767, round(value * 32768)))
                struct.pack_into("<h", converted, output_offset, sample)
                output_offset += 2
            output_handle.writeframes(converted)
            remaining -= chunk_size
        output_handle.close()
    # Give filesystems with coarse timestamp resolution a moment only when the
    # source changed; the final stat check remains the source of truth.
    if _source_signature(source) != signature:
        raise WavCompatibilityError("source WAV changed after reading samples")
