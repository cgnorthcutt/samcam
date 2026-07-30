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


class _MissingArchiveParent(RuntimeError):
    sqlstate = "23503"


class _MissingArchiveAcquire:
    async def __aenter__(self) -> "_MissingArchiveAcquire":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_: object) -> None:
        raise _MissingArchiveParent("archive parent was deleted")


class _MissingArchivePool:
    def acquire(self) -> _MissingArchiveAcquire:
        return _MissingArchiveAcquire()


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

    async def test_deleted_session_tombstone_blocks_local_resync(self) -> None:
        store = ArchiveStore()
        session_id = "Curtis-deleted-tombstone-0001"
        await store.delete_session(session_id)

        await store.start_session({
            "session_id": session_id,
            "started_at": 1_700_000_000,
            "source": "LIVE",
        }, "Curtis")
        await store.save_segment({
            "session_id": session_id,
            "sequence": 0,
            "started_at": 1_700_000_000,
            "duration_seconds": 1,
        }, b"video")

        self.assertNotIn(session_id, store.memory_sessions)
        self.assertEqual(store.memory_segments, {})

    async def test_queued_chunks_for_deleted_parent_do_not_degrade_archive(self) -> None:
        store = ArchiveStore()
        store.database_required = True
        pool = _MissingArchivePool()
        store.pool = pool

        await store.save_segment({
            "session_id": "Curtis-queued-cleanup-0001",
            "sequence": 0,
            "started_at": 1_700_000_000,
            "duration_seconds": 1,
        }, b"video")
        await store.save_recording_chunk({
            "session_id": "Curtis-queued-cleanup-0001",
            "index": 0,
            "count": 1,
            "size_bytes": 5,
        }, b"video")

        self.assertIs(store.pool, pool)
        self.assertIsNone(store.error)

    async def test_capture_device_survives_archive_session_round_trip(self) -> None:
        store = ArchiveStore()
        await store.start_session(
            {
                "session_id": "Curtis-demo-device-0001",
                "started_at": 1_700_000_000,
                "ended_at": 1_700_000_030,
                "source": "Demo · Field walkthrough",
                "capture_device": "Meta Oakley Vanguard AI Glasses",
            },
            "Curtis",
        )

        detail = await store.session_detail("Curtis-demo-device-0001")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["capture_device"], "Meta Oakley Vanguard AI Glasses")

    def test_public_ui_preserves_last_known_archive_list_and_retries_detail(self) -> None:
        page = (Path(__file__).parents[1] / "cloud" / "static" / "index.html").read_text()

        self.assertIn("archiveSessions = null", page)
        self.assertIn("Reconnecting — showing last saved list", page)
        self.assertIn("retryArchiveDetail(sessionId)", page)
        self.assertNotIn("Archive is temporarily unavailable.</div>';", page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
