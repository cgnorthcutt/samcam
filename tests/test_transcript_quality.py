"""Regression tests for rejecting live Whisper feedback hallucinations."""

from __future__ import annotations

import unittest

from transcript_quality import (
    duplicate_transcript_reason,
    normalize_transcript_text,
    transcript_rejection_reason,
)


class TranscriptQualityTests(unittest.TestCase):
    def test_keeps_short_valid_speech(self) -> None:
        text = "Testing, does this work? What the hell?"
        self.assertIsNone(transcript_rejection_reason(text, 3.0))

    def test_rejects_repeated_whisper_phrase(self) -> None:
        phrase = "I'm going to go ahead and get some more of the things I've been doing"
        text = ". ".join([phrase] * 5)
        self.assertIn(
            transcript_rejection_reason(text, 3.0),
            {"impossibly_long_decode", "repeated_phrase"},
        )

    def test_rejects_character_stutter(self) -> None:
        self.assertEqual(
            transcript_rejection_reason("T-" * 40, 3.0),
            "character_stutter",
        )

    def test_suppresses_duplicate_window_but_not_new_short_speech(self) -> None:
        recent = [normalize_transcript_text("Testing, does this work?")]
        self.assertEqual(
            duplicate_transcript_reason("testing does this work", recent),
            "duplicate_window",
        )
        self.assertIsNone(
            duplicate_transcript_reason("what the hell", recent)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
