import pathlib
import struct
import tempfile
import unittest

import sls_audio


def chunk(kind, data):
    return kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")


def wave(*, tag=1, channels=1, rate=8000, bits=16, frames=8000, fact=False):
    align = channels * (bits // 8)
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    if tag != 1:
        fmt += b"\x00\x00"
    payload = b"WAVE" + chunk(b"fmt ", fmt)
    if fact:
        payload += chunk(b"fact", struct.pack("<I", frames))
    payload += chunk(b"data", b"\x00" * frames * align)
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


class WavTests(unittest.TestCase):
    def test_pcm_metadata(self):
        info = sls_audio.inspect_wav_bytes(wave())
        self.assertEqual(info.seconds, 1.0)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.sample_rate, 8000)
        self.assertEqual(info.codec, "PCM")
        self.assertTrue(info.supported)

    def test_mulaw_metadata(self):
        info = sls_audio.inspect_wav_bytes(wave(tag=7, channels=2, bits=8, fact=True))
        self.assertEqual(info.seconds, 1.0)
        self.assertEqual(info.codec, "G.711 mu-law")

    def test_bundled_wavs_are_supported_without_playback(self):
        for path in (pathlib.Path(__file__).parent / "audio").glob("*.wav"):
            with self.subTest(path=path.name):
                info = sls_audio.inspect_wav(path)
                self.assertTrue(info.supported)
                self.assertGreater(info.seconds, 0)

    def test_corrupt_lengths_and_unsupported_codec_are_rejected(self):
        for data in (b"not audio", wave()[:-1], wave() + b"junk", wave(tag=3),
                     wave(frames=0), wave(channels=3), wave(rate=4000), wave(tag=7, bits=16)):
            with self.subTest(length=len(data)):
                with self.assertRaises(sls_audio.AudioValidationError):
                    sls_audio.inspect_wav_bytes(data)

    def test_duration_limit(self):
        with self.assertRaisesRegex(sls_audio.AudioValidationError, "shorter"):
            sls_audio.inspect_wav_bytes(wave(), max_seconds=0.5)

    def test_wrong_block_alignment_and_fact_are_rejected(self):
        bad_alignment = bytearray(wave())
        struct.pack_into("<H", bad_alignment, 32, 99)
        bad_fact = bytearray(wave(tag=7, bits=8, fact=True))
        struct.pack_into("<I", bad_fact, 46, 123)
        for data in (bad_alignment, bad_fact):
            with self.assertRaises(sls_audio.AudioValidationError):
                sls_audio.inspect_wav_bytes(data)

    def test_import_preserves_existing_file_and_rejects_invalid_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.wav"
            destination = pathlib.Path(directory) / "imported.wav"
            source.write_bytes(wave())
            info = sls_audio.copy_validated_audio(source, destination)
            self.assertEqual(info.seconds, 1.0)
            self.assertEqual(source.read_bytes(), destination.read_bytes())
            source.write_bytes(wave(channels=2))
            with self.assertRaises(FileExistsError):
                sls_audio.copy_validated_audio(source, destination)
            self.assertEqual(sls_audio.inspect_wav(destination).channels, 1)
            invalid = pathlib.Path(directory) / "invalid.wav"
            source.write_bytes(b"invalid")
            with self.assertRaises(sls_audio.AudioValidationError):
                sls_audio.copy_validated_audio(source, invalid)
            self.assertFalse(invalid.exists())


if __name__ == "__main__":
    unittest.main()
