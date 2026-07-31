"""Fast deterministic coverage for the compact Analytics audio spectrum."""

from __future__ import annotations

import array
import math
import sys
import unittest

from publish_worker import audio_spectrogram_from_pcm


def pcm_sine(frequency_hz: float, *, sample_rate: int = 16_000, samples: int = 8_192) -> bytes:
    values = array.array(
        "h",
        (round(12_000 * math.sin(2 * math.pi * frequency_hz * index / sample_rate)) for index in range(samples)),
    )
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


class AudioSpectrogramTests(unittest.TestCase):
    def test_high_frequency_tone_is_visible_without_media_processing(self) -> None:
        spectrum = audio_spectrogram_from_pcm(
            pcm_sine(6_000), sample_rate=16_000, steps=8, bins=16
        )

        self.assertTrue(spectrum["available"])
        self.assertEqual(len(spectrum["values"]), 8)
        self.assertTrue(all(len(row) == 16 for row in spectrum["values"]))
        self.assertGreater(spectrum["high_frequency_energy_percent"], 90)
        self.assertEqual(spectrum["near_clip_sample_percent"], 0.0)

    def test_short_or_empty_pcm_has_no_visualization(self) -> None:
        self.assertFalse(audio_spectrogram_from_pcm(b"")["available"])
        self.assertFalse(audio_spectrogram_from_pcm(b"\x00" * 20)["available"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
