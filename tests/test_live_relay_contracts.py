"""Fast deterministic contracts for the final live-demo path.

These tests intentionally exercise no device, network, database, timer, or
media encoder.  They cover the small decisions that previously caused the
most visible demo regressions: classifying the camera mode, accepting safe
relay messages, retaining a complete archive locally, and keeping browser
audio feedback protection in the public UI.
"""

from __future__ import annotations

import importlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud.main import (
    ARCHIVE_MAGIC,
    ARCHIVE_RECORDING_MAGIC,
    LIVE_AUDIO_MAGIC,
    FRAME_FRESH_SECONDS,
    ArchiveStore,
    WorkerState,
    parse_archive_recording_chunk,
    parse_archive_segment,
    parse_live_audio,
    worker_payload,
)


def _envelope(magic: bytes, message_type: str, payload: bytes = b"payload") -> bytes:
    header = json.dumps({"type": message_type, "session_id": "Curtis-contract-0001"}).encode()
    return magic + len(header).to_bytes(4, "big") + header + payload


class UsbModeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # ``server`` normally validates the local ffmpeg executable at import
        # time.  USB classification itself has no ffmpeg dependency, so use a
        # harmless placeholder and keep this regression suite host-independent.
        with patch("shutil.which", return_value="/usr/bin/true"):
            cls.server = importlib.import_module("server")

    def test_generalplus_storage_mode_is_never_reported_as_live(self) -> None:
        devices = [{
            "name": "GENERALPLUS-MSDC",
            "vid": 0x1B3F,
            "pid": 0x8301,
            "classes": [8],
        }]
        with patch.object(self.server, "usb_devices", return_value=devices):
            status = self.server.usb_status()

        self.assertFalse(status["live_capable"])
        self.assertEqual(status["mode"], "mass-storage")
        self.assertEqual(status["id"], "1b3f:8301")

    def test_generalplus_uvc_transition_is_live_capable(self) -> None:
        devices = [{
            "name": "GENERAL - UVC",
            "vid": 0x1B3F,
            "pid": 0x2002,
            "classes": [14, 1],
        }]
        with patch.object(self.server, "usb_devices", return_value=devices):
            status = self.server.usb_status()

        self.assertTrue(status["live_capable"])
        self.assertEqual(status["mode"], "uvc-video")
        self.assertEqual(status["uvc_devices"], ["GENERAL - UVC"])

    def test_an_unrelated_webcam_cannot_activate_the_body_camera_stream(self) -> None:
        devices = [{
            "name": "Other USB Webcam",
            "vid": 0x1234,
            "pid": 0x5678,
            "classes": [14, 1],
        }]
        with patch.object(self.server, "usb_devices", return_value=devices):
            status = self.server.usb_status()

        self.assertFalse(status["live_capable"])
        self.assertNotIn("mode", status)
        self.assertEqual(status["uvc_devices"], ["Other USB Webcam"])


class RelayAndArchiveContracts(unittest.IsolatedAsyncioTestCase):
    async def test_local_archive_lifecycle_keeps_video_audio_text_analytics_and_tombstone(self) -> None:
        store = ArchiveStore()
        # The local fallback is explicitly supported without Postgres.  Force
        # it so this remains deterministic even if a developer has DATABASE_URL
        # set in their shell.
        store.database_required = False
        session_id = "Curtis-contract-0001"
        await store.start_session(
            {
                "session_id": session_id,
                "started_at": 1_700_000_000,
                "ended_at": 1_700_000_010,
                "source": "LIVE · GENERAL - UVC",
                "capture_device": "USB body camera",
            },
            "Curtis",
        )
        await store.save_segment(
            {
                "session_id": session_id,
                "sequence": 0,
                "started_at": 1_700_000_000,
                "duration_seconds": 10,
            },
            b"mp4-part-with-audio",
        )
        await store.save_recording_chunk(
            {"session_id": session_id, "index": 0, "count": 2, "size_bytes": 10}, b"hello"
        )
        await store.save_recording_chunk(
            {"session_id": session_id, "index": 1, "count": 2, "size_bytes": 10}, b"world"
        )
        await store.complete_recording(session_id, 2, 10)
        await store.replace_transcript(session_id, [{"id": "1", "started": 1.25, "text": "Testing works."}])
        await store.save_analytics(session_id, {"clip": {"id": session_id}, "samples": [{"time": 0}]})

        sessions = await store.list_sessions("curtis")
        self.assertEqual([item["session_id"] for item in sessions], [session_id])
        self.assertTrue(sessions[0]["recording_ready"])
        self.assertTrue(sessions[0]["analytics_ready"])
        self.assertEqual(sessions[0]["transcript_count"], 1)

        detail = await store.session_detail(session_id)
        assert detail is not None
        self.assertEqual(detail["capture_device"], "USB body camera")
        self.assertEqual(detail["segments"][0]["sequence"], 0)
        self.assertEqual(detail["transcript"][0]["text"], "Testing works.")
        self.assertEqual((await store.recording(session_id))["data"], b"helloworld")
        self.assertEqual((await store.analytics(session_id))["clip"]["id"], session_id)

        await store.delete_session(session_id)
        self.assertIsNone(await store.session_detail(session_id))
        self.assertIsNone(await store.recording(session_id))
        self.assertIsNone(await store.analytics(session_id))
        # A disconnected publisher may try to replay old local files.  The
        # durable tombstone must still keep the deletion final.
        await store.start_session({"session_id": session_id, "started_at": 1_700_000_020}, "Curtis")
        self.assertEqual(await store.list_sessions("Curtis"), [])

    def test_relay_binary_envelopes_accept_valid_archive_data_and_reject_wrong_types(self) -> None:
        segment = parse_archive_segment(_envelope(ARCHIVE_MAGIC, "archive_segment", b"part"))
        recording = parse_archive_recording_chunk(
            _envelope(ARCHIVE_RECORDING_MAGIC, "archive_recording_chunk", b"chunk")
        )

        self.assertEqual(segment, ({"type": "archive_segment", "session_id": "Curtis-contract-0001"}, b"part"))
        self.assertEqual(recording, ({"type": "archive_recording_chunk", "session_id": "Curtis-contract-0001"}, b"chunk"))
        self.assertIsNone(parse_archive_segment(_envelope(ARCHIVE_MAGIC, "archive_recording_chunk")))
        self.assertIsNone(parse_archive_recording_chunk(_envelope(ARCHIVE_RECORDING_MAGIC, "archive_segment")))
        self.assertIsNone(parse_live_audio(b"not-live-audio"))
        self.assertEqual(parse_live_audio(LIVE_AUDIO_MAGIC + b"\x01\x00"), b"\x01\x00")
        self.assertIsNone(parse_live_audio(LIVE_AUDIO_MAGIC + b"\x01"))

    def test_worker_becomes_offline_when_frames_are_stale_without_erasing_its_identity(self) -> None:
        now = time.time()
        state = WorkerState(
            name="Curtis",
            live=True,
            frame=b"\xff\xd8frame\xff\xd9",
            last_frame_at=now,
            last_seen_at=now,
            active_session_id="Curtis-contract-0001",
            session_started_at=now - 2,
        )
        self.assertTrue(worker_payload(state)["streaming"])

        state.last_frame_at = now - FRAME_FRESH_SECONDS - 0.01
        offline = worker_payload(state)
        self.assertFalse(offline["streaming"])
        self.assertEqual(offline["worker"], "Curtis")
        self.assertIsNone(offline["session_id"])


class PublicAudioUiContracts(unittest.TestCase):
    def test_public_audio_is_guarded_without_a_flickering_status_label(self) -> None:
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn("audioHighPass.frequency.value=120", page)
        self.assertIn("audioLowPass.frequency.value=2800", page)
        self.assertIn("audioLimiter.ratio.value=20", page)
        self.assertIn("audioMasterGain.gain.value=LIVE_AUDIO_SPEAKER_GAIN", page)
        self.assertNotIn('id="audioStatus"', page)
        self.assertIn('<span class="hint">Public demo feed</span>', page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
