"""Regression coverage for progressive public archive playback."""

from __future__ import annotations

import unittest
from pathlib import Path

from cloud.main import ArchiveStore


class ArchivePlaybackContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_playable_parts_remain_listed_when_full_recording_is_ready(self) -> None:
        """The browser can keep playing a part while it prepares the final MP4."""
        store = ArchiveStore()
        session_id = "Curtis-progressive-0001"
        await store.start_session(
            {
                "session_id": session_id,
                "started_at": 1_700_000_000,
                "ended_at": 1_700_000_030,
                "source": "GENERAL - UVC",
            },
            "Curtis",
        )
        await store.save_segment(
            {
                "session_id": session_id,
                "sequence": 0,
                "started_at": 1_700_000_000,
                "duration_seconds": 30,
            },
            b"playable-part",
        )

        preview = await store.session_detail(session_id)
        self.assertFalse(preview["recording_ready"])
        self.assertEqual([part["sequence"] for part in preview["segments"]], [0])

        stitched = b"stitched-video!"
        await store.save_recording_chunk(
            {"session_id": session_id, "index": 0, "count": 1, "size_bytes": len(stitched)},
            stitched,
        )
        await store.complete_recording(session_id, 1, len(stitched))

        final = await store.session_detail(session_id)
        self.assertTrue(final["recording_ready"])
        self.assertEqual([part["sequence"] for part in final["segments"]], [0])

    def test_public_ui_keeps_a_second_player_for_nonblocking_handoff(self) -> None:
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()
        self.assertIn('id="archiveVideoNext"', page)
        self.assertIn("handoffToStitchedRecording", page)
        self.assertIn("renderArchiveParts(detail)", page)

    def test_public_ui_recovers_from_a_transient_archive_video_failure(self) -> None:
        """A 503 after detail success must not leave a gray 0:00 media player."""
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn("function recoverArchiveMedia(video)", page)
        self.assertIn("Saved video will retry automatically.", page)
        self.assertIn("archivePlayers().forEach(video=>video.addEventListener('error'", page)
        self.assertIn("reconnecting full recording", page)
        self.assertIn("retryArchiveDetail(detail.session_id)", page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
