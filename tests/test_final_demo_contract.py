"""Fast, dependency-free guards for the final public demo configuration."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from cloud.main import WorkerState, apply_status, worker_payload


ROOT = Path(__file__).parents[1]


class FinalDemoContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_status_clears_stale_public_video_and_audio(self) -> None:
        """A camera mode switch must never leave old live media exposed."""
        state = WorkerState(name="Curtis")
        token = object()
        state.token = token
        state.live = True
        state.frame = b"jpeg"
        state.last_frame_at = 1_700_000_000
        state.last_seen_at = 1_700_000_000
        state.audio_packets.append((1, b"pcm"))
        state.last_audio_at = 1_700_000_000

        await apply_status(state, token, {"live": False, "source": None})

        self.assertFalse(state.live)
        self.assertIsNone(state.frame)
        self.assertFalse(state.audio_packets)
        self.assertIsNone(state.last_audio_at)
        self.assertFalse(worker_payload(state)["streaming"])

    async def test_stale_connection_cannot_clear_newer_worker_state(self) -> None:
        """A reconnecting publisher must not overwrite a newer websocket."""
        state = WorkerState(name="Curtis")
        current_token = object()
        state.token = current_token
        state.live = True
        state.frame = b"jpeg"
        state.last_frame_at = 1_700_000_000
        state.last_seen_at = 1_700_000_000

        await apply_status(state, object(), {"live": False})

        self.assertTrue(state.live)
        self.assertEqual(state.frame, b"jpeg")

    def test_public_demo_remains_single_instance_and_documents_recovery(self) -> None:
        """Keep live in one process and keep its recovery runbook discoverable."""
        render = (ROOT / "render.yaml").read_text()
        checklist = (ROOT / "docs" / "ROBUSTNESS_CHECKLIST.md").read_text()

        self.assertIn("numInstances: 1", render)
        self.assertIn("archive:\"reconnecting\"", checklist)
        self.assertIn("Worker Curtis is not streaming at this time", checklist)

    def test_native_camera_stall_falls_back_to_verified_ffmpeg_profiles(self) -> None:
        """A UVC helper that yields audio but no frames must not strand Live."""
        server = (ROOT / "server.py").read_text()
        start = server.index("if NATIVE_CAPTURE.exists()")
        end = server.index("for label, input_options in LIVE_CAPTURE_PROFILES")
        native_block = server[start:end]
        self.assertIn('failures.append(f"native AVFoundation:', native_block)
        self.assertNotIn('raise RuntimeError(f"native AVFoundation:', native_block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
