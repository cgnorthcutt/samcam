"""Regression coverage for progressive public archive playback."""

from __future__ import annotations

import unittest
from pathlib import Path

from starlette.requests import Request

from cloud import main as relay
from cloud.main import ArchiveStore


class ArchivePlaybackContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request() -> Request:
        return Request({"type": "http", "method": "GET", "headers": []})

    async def test_public_video_prefers_original_camera_recording(self) -> None:
        store = ArchiveStore()
        session_id = "Curtis-original-preferred-0001"
        await store.start_session(
            {"session_id": session_id, "started_at": 1_700_000_000, "source": "GENERAL - UVC"},
            "Curtis",
        )
        await store.save_recording_chunk(
            {"session_id": session_id, "index": 0, "count": 1, "size_bytes": 6}, b"legacy"
        )
        await store.complete_recording(session_id, 1, 6)
        await store.save_original_recording_chunk(
            {"session_id": session_id, "index": 0, "count": 1, "size_bytes": 8}, b"original"
        )
        await store.complete_original_recording(session_id, 1, 8)

        previous = relay.archive
        relay.archive = store
        try:
            response = await relay.archive_recording(session_id, self._request())
        finally:
            relay.archive = previous
        self.assertEqual(response.body, b"original")

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
        self.assertFalse(final["original_recording_ready"])
        self.assertEqual([part["sequence"] for part in final["segments"]], [0])

        original = b"unmastered-camera-video"
        await store.save_original_recording_chunk(
            {"session_id": session_id, "index": 0, "count": 1, "size_bytes": len(original)},
            original,
        )
        await store.complete_original_recording(session_id, 1, len(original))
        comparison = await store.session_detail(session_id)
        self.assertTrue(comparison["original_recording_ready"])
        self.assertEqual((await store.original_recording(session_id))["data"], original)

    def test_public_ui_keeps_a_second_player_for_nonblocking_handoff(self) -> None:
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()
        self.assertIn('id="archiveVideoNext"', page)
        self.assertIn("handoffToStitchedRecording", page)
        self.assertIn("renderArchiveParts(detail)", page)
        self.assertIn("/archive/${encodeURIComponent(detail.session_id)}/video.mp4", page)
        self.assertNotIn('id="archiveAudioComparison"', page)
        self.assertNotIn("Original camera audio", page)
        self.assertNotIn("Improved archive audio", page)
        self.assertNotIn("renderArchiveAudioComparison", page)

    def test_public_ui_uses_the_video_as_the_only_archive_audio_player(self) -> None:
        """Archive playback has one source of truth: the MP4's camera track."""
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn('id="archiveVideo" controls', page)
        self.assertNotIn("originalArchiveAudio", page)
        self.assertNotIn("improvedArchiveAudio", page)
        self.assertNotIn("archiveAudioVisualizers", page)

    def test_public_ui_recovers_from_a_transient_archive_video_failure(self) -> None:
        """A 503 after detail success must not leave a gray 0:00 media player."""
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn("function recoverArchiveMedia(video)", page)
        self.assertIn("Saved video will retry automatically.", page)
        self.assertIn("video.addEventListener('error',()=>recoverArchiveMedia(video))", page)
        self.assertIn("reconnecting full recording", page)
        self.assertIn("retryArchiveDetail(detail.session_id)", page)

    def test_public_ui_marks_only_device_tagged_archives_as_glasses_captures(self) -> None:
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn('id="archiveDevice"', page)
        self.assertIn("function renderArchiveDevice(detail)", page)
        self.assertIn("Captured with ${device}", page)
        self.assertIn("detail.capture_device", page)

    def test_worker_stream_list_uses_capture_device_icons(self) -> None:
        """Archive labels must distinguish the USB body camera from glasses captures."""
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn("Worker Streams", page)
        self.assertIn("${sessions.length} streams", page)
        self.assertIn("function streamDeviceIcon(stream)", page)
        self.assertIn("/meta|oakley|vanguard/", page)
        self.assertIn("'🥽'", page)
        self.assertIn("'📹'", page)
        self.assertIn("stream.capture_device", page)

    def test_worker_stream_list_can_show_an_explicit_recording_label(self) -> None:
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn("function streamDisplayName(stream)", page)
        self.assertIn("source.match(/\\s—\\s(.+)$/)", page)
        self.assertIn("title.textContent=streamDisplayName(stream)", page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
