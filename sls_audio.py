"""Validate notification sound files before they enter the sound library.

Validation confirms bounded, internally consistent PCM or G.711 mu-law WAV
content. It cannot establish that the user's output device is audible.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import struct


MAX_AUDIO_BYTES = 32 * 1024 * 1024
MAX_AUDIO_SECONDS = 120.0
MAX_CHUNKS = 1024


class AudioValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WavInfo:
    seconds: float
    channels: int
    sample_rate: int
    bits_per_sample: int
    format_tag: int
    data_bytes: int
    supported: bool = True

    @property
    def codec(self) -> str:
        return "PCM" if self.format_tag == 1 else "G.711 mu-law"

    @property
    def samplerate(self) -> int:
        return self.sample_rate


def inspect_wav_bytes(data: bytes, *, max_seconds: float = MAX_AUDIO_SECONDS) -> WavInfo:
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioValidationError("Audio exceeds the 32 MiB limit.")
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioValidationError("Select a RIFF/WAVE sound file.")
    declared_size = struct.unpack_from("<I", data, 4)[0] + 8
    if declared_size != len(data):
        raise AudioValidationError("WAV length does not match its RIFF header.")
    offset, chunks = 12, 0
    fmt: bytes | None = None
    audio_bytes: int | None = None
    sample_count: int | None = None
    while offset < len(data):
        chunks += 1
        if chunks > MAX_CHUNKS or offset + 8 > len(data):
            raise AudioValidationError("WAV contains excessive or truncated chunks.")
        kind, size = struct.unpack_from("<4sI", data, offset)
        start, end = offset + 8, offset + 8 + size
        if end > len(data):
            raise AudioValidationError("WAV contains a truncated chunk.")
        if kind == b"fmt ":
            if fmt is not None or size < 16 or size > 4096:
                raise AudioValidationError("WAV has an invalid or duplicate format chunk.")
            fmt = data[start:end]
        elif kind == b"data":
            if audio_bytes is not None:
                raise AudioValidationError("WAV contains multiple audio streams.")
            audio_bytes = size
        elif kind == b"fact":
            if size < 4 or sample_count is not None:
                raise AudioValidationError("WAV has an invalid sample-count chunk.")
            sample_count = struct.unpack_from("<I", data, start)[0]
        offset = end + (size & 1)
        if offset > len(data):
            raise AudioValidationError("WAV is missing required chunk padding.")
    if fmt is None or audio_bytes is None or audio_bytes == 0:
        raise AudioValidationError("WAV must contain a format and nonempty audio data.")
    tag, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag not in (1, 7):
        raise AudioValidationError("Use PCM or G.711 mu-law WAV audio; this codec is unsupported.")
    if channels not in (1, 2) or not 8000 <= sample_rate <= 192000:
        raise AudioValidationError("Use mono or stereo audio with an 8–192 kHz sample rate.")
    if (tag == 1 and bits not in (8, 16, 24, 32)) or (tag == 7 and bits != 8):
        raise AudioValidationError("WAV bit depth does not match its codec.")
    if len(fmt) == 17 or (len(fmt) >= 18 and struct.unpack_from("<H", fmt, 16)[0] != len(fmt) - 18):
        raise AudioValidationError("WAV format extension length is inconsistent.")
    expected_block = channels * (bits // 8)
    if block_align != expected_block or byte_rate != sample_rate * block_align:
        raise AudioValidationError("WAV sample alignment or byte rate is inconsistent.")
    if audio_bytes % block_align:
        raise AudioValidationError("WAV ends in a partial audio frame.")
    frames = audio_bytes // block_align
    if sample_count is not None and sample_count != frames:
        raise AudioValidationError("WAV sample count does not match its audio data.")
    seconds = frames / sample_rate
    if not 0 < seconds <= max_seconds:
        raise AudioValidationError(f"Notification sounds must be shorter than {max_seconds:g} seconds.")
    return WavInfo(seconds, channels, sample_rate, bits, tag, audio_bytes)


def _read_audio(path: str | os.PathLike[str]) -> bytes:
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioValidationError("Audio exceeds the 32 MiB limit.")
    return data


def inspect_wav(path: str | os.PathLike[str], *, max_seconds: float = MAX_AUDIO_SECONDS) -> WavInfo:
    return inspect_wav_bytes(_read_audio(path), max_seconds=max_seconds)


def copy_validated_audio(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> WavInfo:
    """Copy the exact bytes inspected; exclusive creation never replaces a file.

    The caller selects an approved destination inside its audio library. Invalid
    source data never creates a destination. Failed writes remove only this
    function's newly-created incomplete file.
    """
    data = _read_audio(source)
    info = inspect_wav_bytes(data)
    destination = Path(destination)
    if destination.suffix.lower() != ".wav":
        raise AudioValidationError("The destination must use the .wav extension.")
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    return info
