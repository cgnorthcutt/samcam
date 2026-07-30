"""Regression coverage for source-faithful public demo archive metadata."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import import_demo_archives as importer


class DemoArchiveImportTests(unittest.TestCase):
    def test_verified_demo_timestamps_are_the_original_quicktime_capture_dates(self) -> None:
        demos = {demo.session_id: demo for demo in importer.DEMOS}

        self.assertEqual(
            datetime.fromtimestamp(demos["Curtis-demo-field-walk-20260728"].verified_timestamp, UTC),
            datetime(2026, 7, 25, 22, 6, 39, tzinfo=UTC),
        )
        self.assertEqual(
            datetime.fromtimestamp(demos["Curtis-demo-drive-20260728"].verified_timestamp, UTC),
            datetime(2026, 7, 25, 22, 36, 50, tzinfo=UTC),
        )

    def test_device_quicktime_creationdate_beats_export_creation_time(self) -> None:
        recorded_at = importer.recorded_at_from_tags(
            {
                "creation_time": "2026-07-28T23:25:12Z",
                "com.apple.quicktime.creationdate": "2026-07-25T15:06:39-07:00",
            }
        )

        self.assertEqual(recorded_at, importer.DEMOS[0].verified_timestamp)

    def test_import_is_idempotent_and_preserves_capture_device_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            converted = root / "converted.mp4"
            converted.write_bytes(b"demo")
            demo = replace(
                importer.DEMOS[0],
                converted_path=converted,
                original_filename="missing-original.mov",
            )
            with patch.object(importer, "ARCHIVES", root / "archives"), patch.object(
                importer, "duration_seconds", return_value=73.3
            ):
                importer.import_demo(demo)
                metadata_path = importer.ARCHIVES / demo.session_id / "metadata.json"
                first_metadata = json.loads(metadata_path.read_text())
                importer.import_demo(demo)
                second_metadata = json.loads(metadata_path.read_text())

            self.assertEqual(first_metadata, second_metadata)
            self.assertEqual(first_metadata["started_at"], demo.verified_timestamp)
            self.assertEqual(first_metadata["capture_device"], "Meta Oakley Vanguard AI Glasses")
            self.assertEqual(first_metadata["ended_at"], demo.verified_timestamp + 73.3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
