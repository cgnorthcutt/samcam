"""Fast contracts for isolated public recording replacement."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from republish_archive_recording import (
    ARCHIVE_ORIGINAL_RECORDING_MAGIC,
    ARCHIVE_RECORDING_MAGIC,
    chunk_envelope,
    recording_chunks,
    relay_websocket_url,
    validate_session_id,
)


class RepublishArchiveRecordingTests(unittest.TestCase):
    def test_maintenance_upload_uses_the_archive_binary_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recording = Path(temporary) / "recording.mp4"
            recording.write_bytes(b"verified-mp4-bytes")
            chunks = recording_chunks("Curtis-repair-0001", recording)
            self.assertEqual(len(chunks), 1)
            envelope = chunk_envelope(chunks[0])
            self.assertTrue(envelope.startswith(ARCHIVE_RECORDING_MAGIC))
            header_size = int.from_bytes(envelope[4:8], "big")
            header = json.loads(envelope[8:8 + header_size])
            self.assertEqual(header["type"], "archive_recording_chunk")
            self.assertEqual(header["session_id"], "Curtis-repair-0001")
            self.assertEqual(envelope[8 + header_size:], b"verified-mp4-bytes")

    def test_maintenance_url_is_separate_from_the_live_worker_name(self) -> None:
        self.assertEqual(
            relay_websocket_url("Archive Repair", "https://samcam.app"),
            "wss://samcam.app/ws/worker/Archive%20Repair",
        )

    def test_original_variant_uses_the_distinct_durable_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recording = Path(temporary) / "recording.original.mp4"
            recording.write_bytes(b"original-camera-bytes")
            chunk = recording_chunks("Curtis-repair-0001", recording, variant="original")[0]
            envelope = chunk_envelope(chunk)
            self.assertTrue(envelope.startswith(ARCHIVE_ORIGINAL_RECORDING_MAGIC))
            header_size = int.from_bytes(envelope[4:8], "big")
            header = json.loads(envelope[8:8 + header_size])
            self.assertEqual(header["type"], "archive_original_recording_chunk")

    def test_only_relay_compatible_session_ids_are_accepted(self) -> None:
        self.assertEqual(validate_session_id("Curtis-repair-0001"), "Curtis-repair-0001")
        with self.assertRaises(ValueError):
            validate_session_id("../not-a-session")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
