"""Small deterministic contracts for archive display-title updates."""

from __future__ import annotations

import unittest

from rename_archive_stream import renamed_metadata, validated_title


class ArchiveRenameTests(unittest.TestCase):
    def test_title_is_appended_without_losing_capture_source(self) -> None:
        metadata = {
            "session_id": "Curtis-20260730T073526Z-0ab9b4a3",
            "source": "LIVE · GENERAL - UVC",
            "started_at": 1_785_000_000,
        }
        updated = renamed_metadata(metadata, "audio unsolved")
        self.assertEqual(updated["source"], "LIVE · GENERAL - UVC — audio unsolved")
        self.assertEqual(updated["started_at"], metadata["started_at"])

    def test_replacement_is_idempotent_and_title_is_normalized(self) -> None:
        metadata = {
            "session_id": "Curtis-20260730T062214Z-f0a9dca9",
            "source": "LIVE · GENERAL - UVC — old title",
        }
        updated = renamed_metadata(metadata, "  got audio working now i can sleep  ")
        self.assertEqual(updated["source"], "LIVE · GENERAL - UVC — got audio working now i can sleep")
        self.assertEqual(validated_title(" a   b "), "a b")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
