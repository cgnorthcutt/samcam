"""Regression coverage for archive read failures and browser retry behavior."""

from __future__ import annotations

import unittest
from pathlib import Path

from cloud.main import ArchiveStore, ArchiveUnavailable


class _FailingAcquire:
    async def __aenter__(self) -> object:
        raise RuntimeError("simulated database connection reset")

    async def __aexit__(self, *_: object) -> None:
        return None


class _FailingPool:
    def acquire(self) -> _FailingAcquire:
        return _FailingAcquire()


class ArchiveResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_database_never_silently_serves_memory_as_durable_archive(self) -> None:
        store = ArchiveStore()
        store.database_required = True
        store.error = "archive database unavailable: DNS lookup failed"

        with self.assertRaises(ArchiveUnavailable):
            await store.list_sessions("Curtis")

    async def test_database_read_failure_is_not_reported_as_an_empty_archive(self) -> None:
        store = ArchiveStore()
        store.pool = _FailingPool()

        with self.assertRaises(ArchiveUnavailable):
            await store.list_sessions("Curtis")

    async def test_database_mode_never_retains_uploaded_video_bytes_in_memory(self) -> None:
        store = ArchiveStore()
        store.database_required = True

        await store.save_segment({
            "session_id": "Curtis-memory-guard-0001",
            "sequence": 0,
            "started_at": 1_700_000_000,
            "duration_seconds": 1,
        }, b"video")
        await store.save_recording_chunk({
            "session_id": "Curtis-memory-guard-0001",
            "index": 0,
            "count": 1,
            "size_bytes": 5,
        }, b"video")

        self.assertEqual(store.memory_segments, {})
        self.assertEqual(store.memory_recording_chunks, {})

    def test_public_ui_preserves_last_known_archive_list_and_retries_detail(self) -> None:
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn("archiveSessions = null", page)
        self.assertIn("Reconnecting — showing last saved list", page)
        self.assertIn("retryArchiveDetail(sessionId)", page)
        self.assertNotIn("Archive is temporarily unavailable.</div>';", page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
