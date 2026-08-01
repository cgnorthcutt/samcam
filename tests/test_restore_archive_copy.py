"""Deleted archives can be restored only under an explicit fresh identity."""

from __future__ import annotations

import unittest

from restore_archive_copy import restored_metadata


class RestoreArchiveCopyTests(unittest.TestCase):
    def test_restore_keeps_timing_but_uses_new_identity_and_title(self) -> None:
        source = {
            "session_id": "Curtis-20260730T062214Z-f0a9dca9",
            "started_at": 1_785_392_534.5,
            "ended_at": 1_785_392_574.5,
            "source": "LIVE · GENERAL - UVC — prior title",
        }
        restored = restored_metadata(
            source,
            "Curtis-20260730T062214Z-restored-audio-ready",
            "got audio working now i can sleep",
        )
        self.assertEqual(restored["started_at"], source["started_at"])
        self.assertEqual(restored["ended_at"], source["ended_at"])
        self.assertEqual(restored["source"], "LIVE · GENERAL - UVC — got audio working now i can sleep")
        self.assertEqual(restored["session_id"], "Curtis-20260730T062214Z-restored-audio-ready")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
