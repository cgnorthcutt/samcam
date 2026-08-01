"""Egocentric Camera Lab relay, live transcript, and session archive.

The body camera remains attached to a worker laptop.  The laptop makes one
outbound WebSocket connection to this service, which relays live JPEG frames
and receives compact MP4 archive segments.  Sessions, video segments, and
transcripts are stored in Postgres so archived recordings remain available
after the live publisher disconnects or this service redeploys.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

try:  # Allows local relay tests before its optional database dependency is installed.
    import asyncpg
except ImportError:  # pragma: no cover - exercised only by a minimal local install
    asyncpg = None  # type: ignore[assignment]

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
MAX_FRAME_BYTES = 5_000_000
MAX_LIVE_AUDIO_PACKET_BYTES = 64_000
MAX_ARCHIVE_SEGMENT_BYTES = 8_000_000
MAX_ARCHIVE_RECORDING_CHUNK_BYTES = 1_500_000
FRAME_FRESH_SECONDS = 5.0
PUBLISHER_FRESH_SECONDS = 8.0
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
ARCHIVE_MAGIC = b"SCAS"
ARCHIVE_RECORDING_MAGIC = b"SCAR"
ARCHIVE_ORIGINAL_RECORDING_MAGIC = b"SCOR"
LIVE_AUDIO_MAGIC = b"SCAU"
LIVE_AUDIO_HISTORY_PACKETS = 80
DEFAULT_ARCHIVE_TABLE_PREFIX = "egocapture_archive"
ARCHIVE_TABLE_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,50}$")
ARCHIVE_DATABASE_RETRY_SECONDS = max(
    1.0, float(os.environ.get("EGOCAPTURE_ARCHIVE_DATABASE_RETRY_SECONDS", "5"))
)


class ArchiveUnavailable(RuntimeError):
    """The durable archive could not answer this request right now.

    Returning an empty list for a failed Postgres read makes the browser think
    that a real archive was deleted.  Keep this distinct from a genuinely
    empty archive so callers can preserve their last known-good state.
    """


def require_worker_name(raw_name: str) -> tuple[str, str]:
    """Return a stable key and a human-readable worker name."""
    name = raw_name.strip()
    if not WORKER_NAME.fullmatch(name):
        raise ValueError("worker name must be 1-63 letters, numbers, spaces, . _ or -")
    return name.casefold(), name


def require_session_id(raw_session_id: object) -> str:
    session_id = str(raw_session_id or "").strip()
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid archive session id")
    return session_id


def finite_timestamp(value: object, fallback: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return time.time() if fallback is None else fallback
    return result if result > 0 else (time.time() if fallback is None else fallback)


def session_duration(session: dict[str, Any]) -> float:
    started = finite_timestamp(session.get("started_at"), time.time())
    ended = session.get("ended_at")
    return round(max(0.0, finite_timestamp(ended, time.time()) - started), 2) if ended else round(max(0.0, time.time() - started), 2)


def missing_archive_parent(exc: Exception) -> bool:
    """True for a harmless queued write after its archive was deleted."""
    return getattr(exc, "sqlstate", None) == "23503"


def archive_table_prefix() -> str:
    """Return the archive-table prefix configured for this deployment.

    A deployment can point at an existing archive by setting this environment
    variable.  Keeping that compatibility mapping in deployment configuration
    lets the public project remain independently named while retaining its
    already-persisted recordings.
    """
    prefix = os.environ.get(
        "EGOCAPTURE_ARCHIVE_TABLE_PREFIX", DEFAULT_ARCHIVE_TABLE_PREFIX
    ).strip()
    if not ARCHIVE_TABLE_PREFIX_PATTERN.fullmatch(prefix):
        raise RuntimeError("EGOCAPTURE_ARCHIVE_TABLE_PREFIX is invalid")
    return prefix


class ArchiveConnection:
    """Small asyncpg adapter that applies the configured archive namespace."""

    def __init__(self, connection: Any, table_prefix: str) -> None:
        self._connection = connection
        self._table_prefix = table_prefix

    def _query(self, query: str) -> str:
        return query.replace(DEFAULT_ARCHIVE_TABLE_PREFIX, self._table_prefix)

    async def execute(self, query: str, *args: Any) -> Any:
        return await self._connection.execute(self._query(query), *args)

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._connection.fetch(self._query(query), *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._connection.fetchrow(self._query(query), *args)

    async def executemany(self, query: str, args: Any) -> Any:
        return await self._connection.executemany(self._query(query), args)

    def transaction(self) -> Any:
        return self._connection.transaction()


class ArchiveStore:
    """Postgres-backed archive with an explicit local-development fallback."""

    def __init__(self) -> None:
        self.pool: Any | None = None
        self.error: str | None = None
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        self.table_prefix = archive_table_prefix()
        # No DATABASE_URL is a deliberate local-development configuration. A
        # configured-but-unreachable database is *not* an acceptable fallback
        # for public archive reads: it would make durable sessions disappear.
        self.database_required = bool(self.database_url)
        self.memory_sessions: dict[str, dict[str, Any]] = {}
        self.memory_segments: dict[tuple[str, int], dict[str, Any]] = {}
        self.memory_recording_chunks: dict[tuple[str, int], dict[str, Any]] = {}
        self.memory_recordings: dict[str, dict[str, Any]] = {}
        self.memory_original_recording_chunks: dict[tuple[str, int], dict[str, Any]] = {}
        self.memory_original_recordings: dict[str, dict[str, Any]] = {}
        self.memory_transcripts: dict[tuple[str, str], dict[str, Any]] = {}
        self.memory_analytics: dict[str, dict[str, Any]] = {}
        # A publisher retains local recovery copies.  These durable tombstones
        # stop an explicitly deleted public session from being re-synced.
        self.deleted_session_ids: set[str] = set()

    async def start(self) -> None:
        if not self.database_required:
            self.error = "archive database is not configured"
            return
        if asyncpg is None:
            self.error = "asyncpg is not installed"
            return
        if self.pool is not None:
            return
        candidate: Any | None = None
        try:
            candidate = await asyncpg.create_pool(
                self.database_url, min_size=1, max_size=3, command_timeout=30
            )
            async with candidate.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS egocapture_archive_sessions (
                        session_id TEXT PRIMARY KEY,
                        worker_name TEXT NOT NULL,
                        source TEXT,
                        capture_device TEXT,
                        started_at DOUBLE PRECISION NOT NULL,
                        ended_at DOUBLE PRECISION,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    ALTER TABLE egocapture_archive_sessions
                        ADD COLUMN IF NOT EXISTS capture_device TEXT;
                    CREATE TABLE IF NOT EXISTS egocapture_archive_segments (
                        session_id TEXT NOT NULL REFERENCES egocapture_archive_sessions(session_id) ON DELETE CASCADE,
                        sequence INTEGER NOT NULL,
                        started_at DOUBLE PRECISION NOT NULL,
                        duration_seconds DOUBLE PRECISION NOT NULL,
                        content_type TEXT NOT NULL,
                        data BYTEA NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        PRIMARY KEY (session_id, sequence)
                    );
                    CREATE TABLE IF NOT EXISTS egocapture_archive_transcripts (
                        session_id TEXT NOT NULL REFERENCES egocapture_archive_sessions(session_id) ON DELETE CASCADE,
                        line_key TEXT NOT NULL,
                        started_at DOUBLE PRECISION NOT NULL,
                        received_at DOUBLE PRECISION NOT NULL,
                        text TEXT NOT NULL,
                        PRIMARY KEY (session_id, line_key)
                    );
                    CREATE TABLE IF NOT EXISTS egocapture_archive_recording_chunks (
                        session_id TEXT NOT NULL REFERENCES egocapture_archive_sessions(session_id) ON DELETE CASCADE,
                        chunk_index INTEGER NOT NULL,
                        data BYTEA NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        PRIMARY KEY (session_id, chunk_index)
                    );
                    CREATE TABLE IF NOT EXISTS egocapture_archive_recordings (
                        session_id TEXT PRIMARY KEY REFERENCES egocapture_archive_sessions(session_id) ON DELETE CASCADE,
                        content_type TEXT NOT NULL,
                        chunk_count INTEGER NOT NULL,
                        size_bytes BIGINT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE TABLE IF NOT EXISTS egocapture_archive_original_recording_chunks (
                        session_id TEXT NOT NULL REFERENCES egocapture_archive_sessions(session_id) ON DELETE CASCADE,
                        chunk_index INTEGER NOT NULL,
                        data BYTEA NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        PRIMARY KEY (session_id, chunk_index)
                    );
                    CREATE TABLE IF NOT EXISTS egocapture_archive_original_recordings (
                        session_id TEXT PRIMARY KEY REFERENCES egocapture_archive_sessions(session_id) ON DELETE CASCADE,
                        content_type TEXT NOT NULL,
                        chunk_count INTEGER NOT NULL,
                        size_bytes BIGINT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE TABLE IF NOT EXISTS egocapture_archive_analytics (
                        session_id TEXT PRIMARY KEY REFERENCES egocapture_archive_sessions(session_id) ON DELETE CASCADE,
                        payload JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE TABLE IF NOT EXISTS egocapture_archive_deleted_sessions (
                        session_id TEXT PRIMARY KEY,
                        deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS egocapture_archive_sessions_worker_idx
                        ON egocapture_archive_sessions (worker_name, started_at DESC);
                    CREATE INDEX IF NOT EXISTS egocapture_archive_transcripts_session_idx
                        ON egocapture_archive_transcripts (session_id, started_at);
                    """
                )
                deleted = await connection.fetch(
                    "SELECT session_id FROM egocapture_archive_deleted_sessions"
                )
                self.deleted_session_ids = {str(row["session_id"]) for row in deleted}
            self.pool = candidate
            self.error = None
            # A boot-time database DNS miss must not discard what the active
            # publisher already delivered to this process. Replay its bounded
            # in-memory copy as soon as durable storage comes back.
            await self._flush_memory_to_database()
        except Exception as exc:  # noqa: BLE001 - the live relay must survive an archive outage
            if candidate is not None:
                await candidate.close()
            self.pool = None
            self.error = f"archive database unavailable: {exc}"[-300:]
            print(self.error, flush=True)

    async def _flush_memory_to_database(self) -> None:
        """Persist data accepted while a configured database was reconnecting."""
        if self.pool is None:
            return
        sessions = [dict(record) for record in self.memory_sessions.values()]
        segments = [dict(record) for record in self.memory_segments.values()]
        recording_chunks = [dict(record) for record in self.memory_recording_chunks.values()]
        recordings = [dict(record) for record in self.memory_recordings.values()]
        original_recording_chunks = [dict(record) for record in self.memory_original_recording_chunks.values()]
        original_recordings = [dict(record) for record in self.memory_original_recordings.values()]
        transcripts = list(self.memory_transcripts.values())
        analytics = dict(self.memory_analytics)

        for record in sessions:
            await self.start_session(record, str(record["worker_name"]))
        for record in segments:
            await self.save_segment(record, bytes(record["data"]))
        for record in recording_chunks:
            await self.save_recording_chunk(
                {
                    "session_id": record["session_id"],
                    "index": record["index"],
                    "count": record["count"],
                    "size_bytes": record["total_size"],
                },
                bytes(record["data"]),
            )
        for record in recordings:
            await self.complete_recording(
                str(record["session_id"]),
                int(record["chunk_count"]),
                int(record["size_bytes"]),
            )
        for record in original_recording_chunks:
            await self.save_original_recording_chunk(
                {
                    "session_id": record["session_id"],
                    "index": record["index"],
                    "count": record["count"],
                    "size_bytes": record["total_size"],
                },
                bytes(record["data"]),
            )
        for record in original_recordings:
            await self.complete_original_recording(
                str(record["session_id"]),
                int(record["chunk_count"]),
                int(record["size_bytes"]),
            )
        by_session: dict[str, list[dict[str, Any]]] = {}
        for record in transcripts:
            by_session.setdefault(str(record["session_id"]), []).append(dict(record))
        for session_id, lines in by_session.items():
            await self.replace_transcript(session_id, lines)
        for session_id, payload in analytics.items():
            await self.save_analytics(session_id, payload)

    async def reconnect_forever(self) -> None:
        """Reconnect a configured Postgres archive without interrupting live relay."""
        while self.database_required:
            if self.pool is None:
                await self.start()
            await asyncio.sleep(ARCHIVE_DATABASE_RETRY_SECONDS)

    async def _mark_database_unavailable(self, message: str) -> None:
        """Drop a failed pool so the reconnect loop can replace it cleanly."""
        self.error = message[-300:]
        pool, self.pool = self.pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001 - original read error is clearer
                pass

    async def close(self) -> None:
        pool, self.pool = self.pool, None
        if pool is not None:
            await pool.close()

    async def start_session(self, session: dict[str, Any], worker_name: str) -> None:
        session_id = require_session_id(session.get("session_id"))
        if session_id in self.deleted_session_ids:
            return
        record = {
            "session_id": session_id,
            "worker_name": worker_name,
            "source": str(session.get("source") or "")[:160] or None,
            "capture_device": str(session.get("capture_device") or "")[:160] or None,
            "started_at": finite_timestamp(session.get("started_at")),
            "ended_at": finite_timestamp(session["ended_at"]) if session.get("ended_at") else None,
        }
        # The in-memory archive is strictly a local-development fallback.  On
        # Render, Postgres is the durable source of truth and retaining every
        # uploaded session here would keep duplicate video bytes in RAM until
        # the process is killed.
        if not self.database_required:
            prior = self.memory_sessions.get(session_id)
            self.memory_sessions[session_id] = {**(prior or {}), **record}
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                await connection.execute(
                    """
                    INSERT INTO egocapture_archive_sessions
                      (session_id, worker_name, source, capture_device, started_at, ended_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (session_id) DO UPDATE SET
                      worker_name = EXCLUDED.worker_name,
                      source = COALESCE(EXCLUDED.source, egocapture_archive_sessions.source),
                      capture_device = COALESCE(EXCLUDED.capture_device, egocapture_archive_sessions.capture_device),
                      started_at = LEAST(EXCLUDED.started_at, egocapture_archive_sessions.started_at),
                      -- A new session_start has no end time and resumes an
                      -- active stream after a relay restart. Archived-session
                      -- sync carries an end time and keeps it finalized.
                      ended_at = EXCLUDED.ended_at
                    """,
                    record["session_id"], record["worker_name"], record["source"], record["capture_device"],
                    record["started_at"], record["ended_at"],
                )
        except Exception as exc:  # noqa: BLE001
            message = f"archive write failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    async def end_session(self, session_id: str, ended_at: float | None = None) -> None:
        session_id = require_session_id(session_id)
        if session_id in self.deleted_session_ids:
            return
        ended_at = finite_timestamp(ended_at)
        if not self.database_required and session_id in self.memory_sessions:
            self.memory_sessions[session_id]["ended_at"] = ended_at
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                await connection.execute(
                    "UPDATE egocapture_archive_sessions SET ended_at = $2 WHERE session_id = $1",
                    session_id, ended_at,
                )
        except Exception as exc:  # noqa: BLE001
            message = f"archive update failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    async def save_segment(self, metadata: dict[str, Any], data: bytes) -> None:
        session_id = require_session_id(metadata.get("session_id"))
        if session_id in self.deleted_session_ids:
            return
        sequence = int(metadata.get("sequence", -1))
        if sequence < 0 or len(data) > MAX_ARCHIVE_SEGMENT_BYTES:
            return
        record = {
            "session_id": session_id,
            "sequence": sequence,
            "started_at": finite_timestamp(metadata.get("started_at")),
            "duration_seconds": round(max(0.0, float(metadata.get("duration_seconds", 0))), 2),
            "content_type": "video/mp4",
            "size_bytes": len(data),
            "data": data,
        }
        if not self.database_required:
            self.memory_segments[(session_id, sequence)] = record
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                await connection.execute(
                    """
                    INSERT INTO egocapture_archive_segments
                      (session_id, sequence, started_at, duration_seconds, content_type, data, size_bytes)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (session_id, sequence) DO NOTHING
                    """,
                    record["session_id"], record["sequence"], record["started_at"], record["duration_seconds"],
                    record["content_type"], record["data"], record["size_bytes"],
                )
        except Exception as exc:  # noqa: BLE001
            # A publisher can have already queued a part when a concurrent
            # archive deletion removes its parent session. That is expected
            # cleanup, not a database outage.
            if missing_archive_parent(exc):
                return
            message = f"archive segment failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    async def _save_recording_chunk(
        self, metadata: dict[str, Any], data: bytes, *, original: bool = False
    ) -> None:
        """Save one byte range of a finished archive MP4 variant."""
        session_id = require_session_id(metadata.get("session_id"))
        if session_id in self.deleted_session_ids:
            return
        index = int(metadata.get("index", -1))
        count = int(metadata.get("count", 0))
        total_size = int(metadata.get("size_bytes", 0))
        if (
            index < 0 or count <= 0 or index >= count or count > 100_000
            or total_size <= 0 or len(data) <= 0 or len(data) > MAX_ARCHIVE_RECORDING_CHUNK_BYTES
        ):
            return
        record = {
            "session_id": session_id,
            "index": index,
            "count": count,
            "total_size": total_size,
            "size_bytes": len(data),
            "data": data,
        }
        memory_chunks = (
            self.memory_original_recording_chunks if original else self.memory_recording_chunks
        )
        table = (
            "egocapture_archive_original_recording_chunks"
            if original else "egocapture_archive_recording_chunks"
        )
        label = "original archive recording" if original else "archive recording"
        if not self.database_required:
            memory_chunks[(session_id, index)] = record
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                await connection.execute(
                    f"""
                    INSERT INTO {table} (session_id, chunk_index, data, size_bytes)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (session_id, chunk_index) DO UPDATE SET
                      data = EXCLUDED.data,
                      size_bytes = EXCLUDED.size_bytes
                    """,
                    session_id, index, data, len(data),
                )
        except Exception as exc:  # noqa: BLE001
            if missing_archive_parent(exc):
                return
            message = f"{label} chunk failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    async def save_recording_chunk(self, metadata: dict[str, Any], data: bytes) -> None:
        """Save one byte range of the primary archive MP4 recording."""
        await self._save_recording_chunk(metadata, data, original=False)

    async def save_original_recording_chunk(self, metadata: dict[str, Any], data: bytes) -> None:
        """Save one byte range of the unmastered camera MP4 recording."""
        await self._save_recording_chunk(metadata, data, original=True)

    async def _complete_recording(
        self, session_id: str, chunk_count: int, size_bytes: int, *, original: bool = False
    ) -> None:
        session_id = require_session_id(session_id)
        if session_id in self.deleted_session_ids:
            return
        if chunk_count <= 0 or chunk_count > 100_000 or size_bytes <= 0:
            return
        memory_chunks = (
            self.memory_original_recording_chunks if original else self.memory_recording_chunks
        )
        memory_recordings = (
            self.memory_original_recordings if original else self.memory_recordings
        )
        chunks_table = (
            "egocapture_archive_original_recording_chunks"
            if original else "egocapture_archive_recording_chunks"
        )
        recordings_table = (
            "egocapture_archive_original_recordings"
            if original else "egocapture_archive_recordings"
        )
        label = "original archive recording" if original else "archive recording"
        if not self.database_required:
            memory_parts = [
                value for (candidate, _), value in memory_chunks.items() if candidate == session_id
            ]
            if len(memory_parts) == chunk_count and sum(int(part["size_bytes"]) for part in memory_parts) == size_bytes:
                memory_recordings[session_id] = {
                    "session_id": session_id,
                    "content_type": "video/mp4",
                    "chunk_count": chunk_count,
                    "size_bytes": size_bytes,
                }
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                async with connection.transaction():
                    actual = await connection.fetchrow(
                        f"""
                        SELECT COUNT(*)::int AS chunk_count, COALESCE(SUM(size_bytes), 0)::bigint AS size_bytes
                        FROM {chunks_table}
                        WHERE session_id = $1
                        """,
                        session_id,
                    )
                    if actual is None or int(actual["chunk_count"]) != chunk_count or int(actual["size_bytes"]) != size_bytes:
                        return
                    await connection.execute(
                        f"""
                        INSERT INTO {recordings_table} (session_id, content_type, chunk_count, size_bytes)
                        VALUES ($1, 'video/mp4', $2, $3)
                        ON CONFLICT (session_id) DO UPDATE SET
                          content_type = EXCLUDED.content_type,
                          chunk_count = EXCLUDED.chunk_count,
                          size_bytes = EXCLUDED.size_bytes,
                          created_at = NOW()
                        """,
                        session_id, chunk_count, size_bytes,
                    )
        except Exception as exc:  # noqa: BLE001
            message = f"{label} completion failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    async def complete_recording(self, session_id: str, chunk_count: int, size_bytes: int) -> None:
        """Mark the primary archive recording as complete after all chunks arrive."""
        await self._complete_recording(session_id, chunk_count, size_bytes, original=False)

    async def complete_original_recording(self, session_id: str, chunk_count: int, size_bytes: int) -> None:
        """Mark the original camera recording as complete after all chunks arrive."""
        await self._complete_recording(session_id, chunk_count, size_bytes, original=True)

    async def save_analytics(self, session_id: str, payload: dict[str, Any]) -> None:
        """Store video-derived per-session analytics sent by the local publisher."""
        session_id = require_session_id(session_id)
        if session_id in self.deleted_session_ids:
            return
        samples = payload.get("samples")
        clip = payload.get("clip")
        if not isinstance(samples, list) or not samples or len(samples) > 500 or not isinstance(clip, dict):
            return
        try:
            serialized = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        if len(serialized.encode()) > 1_000_000:
            return
        if not self.database_required:
            self.memory_analytics[session_id] = payload
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                await connection.execute(
                    """
                    INSERT INTO egocapture_archive_analytics (session_id, payload)
                    VALUES ($1, $2::jsonb)
                    ON CONFLICT (session_id) DO UPDATE SET
                      payload = EXCLUDED.payload,
                      updated_at = NOW()
                    """,
                    session_id, serialized,
                )
        except Exception as exc:  # noqa: BLE001
            message = f"archive analytics write failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    async def analytics(self, session_id: str) -> dict[str, Any] | None:
        session_id = require_session_id(session_id)
        if self.pool is None:
            if self.database_required:
                raise ArchiveUnavailable(
                    self.error or "archive database is reconnecting"
                )
            return self.memory_analytics.get(session_id)
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                row = await connection.fetchrow(
                    "SELECT payload FROM egocapture_archive_analytics WHERE session_id = $1", session_id
                )
            if row is None:
                return None
            payload = row["payload"]
            # asyncpg returns JSONB as a decoded object with some codecs and as
            # a JSON string with others.  Accept both so a persisted analytics
            # row that is visible in the archive list is also usable by the
            # public per-video analytics endpoint.
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    return None
            return dict(payload) if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001
            message = f"archive analytics read failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)
            raise ArchiveUnavailable(message) from exc

    async def delete_session(self, session_id: str) -> None:
        """Permanently remove one archived session and all of its artifacts."""
        session_id = require_session_id(session_id)
        self.deleted_session_ids.add(session_id)
        self.memory_sessions.pop(session_id, None)
        self.memory_recordings.pop(session_id, None)
        self.memory_original_recordings.pop(session_id, None)
        self.memory_analytics.pop(session_id, None)
        self.memory_segments = {
            key: value for key, value in self.memory_segments.items() if key[0] != session_id
        }
        self.memory_recording_chunks = {
            key: value for key, value in self.memory_recording_chunks.items() if key[0] != session_id
        }
        self.memory_original_recording_chunks = {
            key: value for key, value in self.memory_original_recording_chunks.items() if key[0] != session_id
        }
        self.memory_transcripts = {
            key: value for key, value in self.memory_transcripts.items() if key[0] != session_id
        }
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO egocapture_archive_deleted_sessions (session_id)
                        VALUES ($1)
                        ON CONFLICT (session_id) DO NOTHING
                        """,
                        session_id,
                    )
                    await connection.execute("DELETE FROM egocapture_archive_sessions WHERE session_id = $1", session_id)
        except Exception as exc:  # noqa: BLE001
            self.error = f"archive deletion failed: {exc}"[-300:]
            print(self.error, flush=True)

    async def _recording(self, session_id: str, *, original: bool = False) -> dict[str, Any] | None:
        session_id = require_session_id(session_id)
        memory_recordings = self.memory_original_recordings if original else self.memory_recordings
        memory_chunks = self.memory_original_recording_chunks if original else self.memory_recording_chunks
        recordings_table = "egocapture_archive_original_recordings" if original else "egocapture_archive_recordings"
        chunks_table = "egocapture_archive_original_recording_chunks" if original else "egocapture_archive_recording_chunks"
        label = "original archive recording" if original else "archive recording"
        if self.pool is None:
            if self.database_required:
                raise ArchiveUnavailable(
                    self.error or "archive database is reconnecting"
                )
            metadata = memory_recordings.get(session_id)
            if metadata is None:
                return None
            parts = [
                value for (candidate, _), value in sorted(memory_chunks.items()) if candidate == session_id
            ]
            if len(parts) != int(metadata["chunk_count"]):
                return None
            data = b"".join(part["data"] for part in parts)
            if len(data) != int(metadata["size_bytes"]):
                return None
            return {**metadata, "data": data}
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                metadata = await connection.fetchrow(
                    f"SELECT content_type, chunk_count, size_bytes FROM {recordings_table} WHERE session_id = $1",
                    session_id,
                )
                if metadata is None:
                    return None
                rows = await connection.fetch(
                    f"SELECT data FROM {chunks_table} WHERE session_id = $1 ORDER BY chunk_index",
                    session_id,
                )
            if len(rows) != int(metadata["chunk_count"]):
                return None
            data = b"".join(row["data"] for row in rows)
            if len(data) != int(metadata["size_bytes"]):
                return None
            return {**dict(metadata), "data": data}
        except Exception as exc:  # noqa: BLE001
            message = f"{label} read failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)
            raise ArchiveUnavailable(message) from exc

    async def recording(self, session_id: str) -> dict[str, Any] | None:
        """Return the legacy primary archive MP4 when no original is available."""
        return await self._recording(session_id, original=False)

    async def original_recording(self, session_id: str) -> dict[str, Any] | None:
        """Return the preferred, unmodified camera MP4."""
        return await self._recording(session_id, original=True)

    async def save_transcript(self, session_id: str, line: dict[str, Any]) -> None:
        session_id = require_session_id(session_id)
        if session_id in self.deleted_session_ids:
            return
        text = " ".join(str(line.get("text", "")).split())[:1_000]
        if not text:
            return
        started_at = max(0.0, finite_timestamp(line.get("started"), 0.0))
        line_key = str(line.get("id") or f"{started_at:.2f}:{text}")[:200]
        record = {
            "session_id": session_id,
            "line_key": line_key,
            "text": text,
            "started_at": started_at,
            "received_at": finite_timestamp(line.get("received_at")),
        }
        if not self.database_required:
            self.memory_transcripts[(session_id, line_key)] = record
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                await connection.execute(
                    """
                    INSERT INTO egocapture_archive_transcripts
                      (session_id, line_key, started_at, received_at, text)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (session_id, line_key) DO NOTHING
                    """,
                    record["session_id"], record["line_key"], record["started_at"], record["received_at"], record["text"],
                )
        except Exception as exc:  # noqa: BLE001
            message = f"archive transcript failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    async def replace_transcript(self, session_id: str, lines: list[dict[str, Any]]) -> None:
        """Synchronize one archived session from the publisher's local source of truth."""
        session_id = require_session_id(session_id)
        if session_id in self.deleted_session_ids:
            return
        records: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for line in lines[:1_000]:
            text = " ".join(str(line.get("text", "")).split())[:1_000]
            if not text:
                continue
            started_at = max(0.0, finite_timestamp(line.get("started"), 0.0))
            line_key = str(line.get("id") or f"{started_at:.2f}:{text}")[:200]
            if line_key in seen_keys:
                continue
            seen_keys.add(line_key)
            records.append({
                "session_id": session_id,
                "line_key": line_key,
                "text": text,
                "started_at": started_at,
                "received_at": finite_timestamp(line.get("received_at")),
            })

        if not self.database_required:
            self.memory_transcripts = {
                key: record for key, record in self.memory_transcripts.items() if key[0] != session_id
            }
            self.memory_transcripts.update({(session_id, record["line_key"]): record for record in records})
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                async with connection.transaction():
                    await connection.execute(
                        "DELETE FROM egocapture_archive_transcripts WHERE session_id = $1", session_id
                    )
                    if records:
                        await connection.executemany(
                            """
                            INSERT INTO egocapture_archive_transcripts
                              (session_id, line_key, started_at, received_at, text)
                            VALUES ($1, $2, $3, $4, $5)
                            """,
                            [
                                (
                                    record["session_id"], record["line_key"], record["started_at"],
                                    record["received_at"], record["text"],
                                )
                                for record in records
                            ],
                        )
        except Exception as exc:  # noqa: BLE001
            message = f"archive transcript sync failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)

    def _memory_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        session_id = record["session_id"]
        segments = [value for (candidate, _), value in self.memory_segments.items() if candidate == session_id]
        transcripts = [value for (candidate, _), value in self.memory_transcripts.items() if candidate == session_id]
        recording = self.memory_recordings.get(session_id)
        original_recording = self.memory_original_recordings.get(session_id)
        return {
            **record,
            "duration_seconds": session_duration(record),
            "segment_count": len(segments),
            "size_bytes": sum(int(item["size_bytes"]) for item in segments),
            "recording_ready": recording is not None,
            "recording_size_bytes": int(recording["size_bytes"]) if recording else 0,
            "original_recording_ready": original_recording is not None,
            "original_recording_size_bytes": int(original_recording["size_bytes"]) if original_recording else 0,
            "transcript_count": len(transcripts),
            "analytics_ready": session_id in self.memory_analytics,
        }

    async def list_sessions(self, worker_name: str) -> list[dict[str, Any]]:
        if self.pool is None:
            if self.database_required:
                raise ArchiveUnavailable(
                    self.error or "archive database is reconnecting"
                )
            return sorted(
                [
                    self._memory_summary(record)
                    for record in self.memory_sessions.values()
                    if record["worker_name"].casefold() == worker_name.casefold()
                    and (
                        record.get("ended_at") is None
                        or any(candidate == record["session_id"] for candidate, _ in self.memory_segments)
                        or record["session_id"] in self.memory_recordings
                        or record["session_id"] in self.memory_original_recordings
                    )
                ],
                key=lambda item: float(item["started_at"]), reverse=True,
            )
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                rows = await connection.fetch(
                    """
                    SELECT s.session_id, s.worker_name, s.source, s.capture_device, s.started_at, s.ended_at,
                      COALESCE((SELECT COUNT(*) FROM egocapture_archive_segments g WHERE g.session_id = s.session_id), 0) AS segment_count,
                      COALESCE((SELECT SUM(g.size_bytes) FROM egocapture_archive_segments g WHERE g.session_id = s.session_id), 0) AS size_bytes,
                      EXISTS(SELECT 1 FROM egocapture_archive_recordings r WHERE r.session_id = s.session_id) AS recording_ready,
                      COALESCE((SELECT r.size_bytes FROM egocapture_archive_recordings r WHERE r.session_id = s.session_id), 0) AS recording_size_bytes,
                      EXISTS(SELECT 1 FROM egocapture_archive_original_recordings r WHERE r.session_id = s.session_id) AS original_recording_ready,
                      COALESCE((SELECT r.size_bytes FROM egocapture_archive_original_recordings r WHERE r.session_id = s.session_id), 0) AS original_recording_size_bytes,
                      EXISTS(SELECT 1 FROM egocapture_archive_analytics a WHERE a.session_id = s.session_id) AS analytics_ready,
                      COALESCE((SELECT COUNT(*) FROM egocapture_archive_transcripts t WHERE t.session_id = s.session_id), 0) AS transcript_count
                    FROM egocapture_archive_sessions s
                    WHERE LOWER(s.worker_name) = LOWER($1)
                      AND (s.ended_at IS NULL OR EXISTS (
                        SELECT 1 FROM egocapture_archive_segments g WHERE g.session_id = s.session_id
                      ) OR EXISTS (
                        SELECT 1 FROM egocapture_archive_recordings r WHERE r.session_id = s.session_id
                      ) OR EXISTS (
                        SELECT 1 FROM egocapture_archive_original_recordings r WHERE r.session_id = s.session_id
                      ))
                    ORDER BY s.started_at DESC
                    LIMIT 100
                    """, worker_name,
                )
            sessions = [dict(row) for row in rows]
            for record in sessions:
                record["duration_seconds"] = session_duration(record)
            return sessions
        except Exception as exc:  # noqa: BLE001
            message = f"archive read failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)
            raise ArchiveUnavailable(message) from exc

    async def session_detail(self, session_id: str) -> dict[str, Any] | None:
        session_id = require_session_id(session_id)
        if self.pool is None:
            if self.database_required:
                raise ArchiveUnavailable(
                    self.error or "archive database is reconnecting"
                )
            session = self.memory_sessions.get(session_id)
            if session is None:
                return None
            result = self._memory_summary(session)
            result["segments"] = [
                {key: value for key, value in item.items() if key != "data"}
                for (_, _), item in sorted(self.memory_segments.items()) if item["session_id"] == session_id
            ]
            result["transcript"] = [
                {"text": item["text"], "started": item["started_at"], "received_at": item["received_at"]}
                for (_, _), item in sorted(self.memory_transcripts.items(), key=lambda item: item[1]["started_at"])
                if item["session_id"] == session_id
            ]
            return result
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                session = await connection.fetchrow(
                    """
                    SELECT s.session_id, s.worker_name, s.source, s.capture_device, s.started_at, s.ended_at,
                      EXISTS(SELECT 1 FROM egocapture_archive_recordings r WHERE r.session_id = s.session_id) AS recording_ready,
                      COALESCE((SELECT r.size_bytes FROM egocapture_archive_recordings r WHERE r.session_id = s.session_id), 0) AS recording_size_bytes,
                      EXISTS(SELECT 1 FROM egocapture_archive_original_recordings r WHERE r.session_id = s.session_id) AS original_recording_ready,
                      COALESCE((SELECT r.size_bytes FROM egocapture_archive_original_recordings r WHERE r.session_id = s.session_id), 0) AS original_recording_size_bytes,
                      EXISTS(SELECT 1 FROM egocapture_archive_analytics a WHERE a.session_id = s.session_id) AS analytics_ready
                    FROM egocapture_archive_sessions s
                    WHERE s.session_id = $1
                    """,
                    session_id,
                )
                if session is None:
                    return None
                segments = await connection.fetch(
                    "SELECT sequence, started_at, duration_seconds, size_bytes FROM egocapture_archive_segments WHERE session_id = $1 ORDER BY sequence", session_id
                )
                transcript = await connection.fetch(
                    "SELECT text, started_at AS started, received_at FROM egocapture_archive_transcripts WHERE session_id = $1 ORDER BY started_at", session_id
                )
            result = dict(session)
            result["segments"] = [dict(row) for row in segments]
            result["transcript"] = [dict(row) for row in transcript]
            result["segment_count"] = len(result["segments"])
            result["size_bytes"] = sum(int(item["size_bytes"]) for item in result["segments"])
            result["transcript_count"] = len(result["transcript"])
            result["duration_seconds"] = session_duration(result)
            return result
        except Exception as exc:  # noqa: BLE001
            message = f"archive detail failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)
            raise ArchiveUnavailable(message) from exc

    async def segment(self, session_id: str, sequence: int) -> dict[str, Any] | None:
        session_id = require_session_id(session_id)
        if sequence < 0:
            return None
        if self.pool is None:
            if self.database_required:
                raise ArchiveUnavailable(
                    self.error or "archive database is reconnecting"
                )
            return self.memory_segments.get((session_id, sequence))
        try:
            async with self.pool.acquire() as raw_connection:
                connection = ArchiveConnection(raw_connection, self.table_prefix)
                row = await connection.fetchrow(
                    "SELECT content_type, data, size_bytes FROM egocapture_archive_segments WHERE session_id = $1 AND sequence = $2",
                    session_id, sequence,
                )
            return dict(row) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            message = f"archive segment read failed: {exc}"[-300:]
            print(message, flush=True)
            await self._mark_database_unavailable(message)
            raise ArchiveUnavailable(message) from exc


@dataclass
class WorkerState:
    name: str
    live: bool = False
    source: str | None = None
    frame: bytes | None = None
    frame_sequence: int = 0
    last_frame_at: float | None = None
    audio_sequence: int = 0
    audio_packets: deque[tuple[int, bytes]] = field(
        default_factory=lambda: deque(maxlen=LIVE_AUDIO_HISTORY_PACKETS)
    )
    last_audio_at: float | None = None
    last_seen_at: float | None = None
    connected_at: float | None = None
    active_session_id: str | None = None
    session_started_at: float | None = None
    transcripts: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    transcript_fingerprints: deque[str] = field(default_factory=lambda: deque(maxlen=300))
    token: object | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_streaming(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(
            self.live and self.frame is not None and self.last_frame_at is not None
            and now - self.last_frame_at < FRAME_FRESH_SECONDS
            and self.last_seen_at is not None and now - self.last_seen_at < PUBLISHER_FRESH_SECONDS
        )


archive = ArchiveStore()
workers: dict[str, WorkerState] = {}
workers_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await archive.start()
    reconnect_task = (
        asyncio.create_task(archive.reconnect_forever())
        if archive.database_required
        else None
    )
    try:
        yield
    finally:
        if reconnect_task is not None:
            reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconnect_task
        await archive.close()


app = FastAPI(title="Egocentric Camera Lab Relay", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


async def worker_for_publisher(raw_name: str) -> tuple[str, WorkerState]:
    key, display_name = require_worker_name(raw_name)
    async with workers_lock:
        state = workers.get(key)
        if state is None:
            state = WorkerState(name=display_name)
            workers[key] = state
        return key, state


async def existing_worker(raw_name: str) -> WorkerState | None:
    try:
        key, _ = require_worker_name(raw_name)
    except ValueError:
        return None
    async with workers_lock:
        return workers.get(key)


def worker_payload(state: WorkerState | None) -> dict[str, Any]:
    if state is None:
        return {"worker": None, "streaming": False, "last_frame_at": None, "audio_available": False, "source": None, "session_id": None, "transcript_count": 0}
    now = time.time()
    streaming = state.is_streaming(now)
    return {
        "worker": state.name,
        "streaming": streaming,
        "last_frame_at": state.last_frame_at,
        "audio_available": bool(
            state.last_audio_at is not None and now - state.last_audio_at < FRAME_FRESH_SECONDS
        ),
        "source": state.source,
        "connected_at": state.connected_at,
        "session_id": state.active_session_id if streaming else None,
        "session_started_at": state.session_started_at if streaming else None,
        "transcript_count": len(state.transcripts) if streaming else 0,
    }


async def apply_status(state: WorkerState, token: object, message: dict[str, Any]) -> None:
    async with state.lock:
        if state.token is not token:
            return
        state.last_seen_at = time.time()
        state.live = bool(message.get("live"))
        source = message.get("source")
        state.source = str(source)[:160] if source else None
        if not state.live:
            state.frame = None
            state.audio_packets.clear()
            state.last_audio_at = None


async def apply_session_start(state: WorkerState, token: object, message: dict[str, Any]) -> None:
    raw_session = message.get("session") if isinstance(message.get("session"), dict) else message
    try:
        session_id = require_session_id(raw_session.get("session_id"))
    except (AttributeError, ValueError):
        return
    started_at = finite_timestamp(raw_session.get("started_at"))
    source = str(raw_session.get("source") or "")[:160] or None
    capture_device = str(raw_session.get("capture_device") or "")[:160] or None
    async with state.lock:
        if state.token is not token:
            return
        state.active_session_id = session_id
        state.session_started_at = started_at
        state.transcripts.clear()
        state.transcript_fingerprints.clear()
        state.audio_packets.clear()
        state.last_audio_at = None
        if source:
            state.source = source
        state.last_seen_at = time.time()
    await archive.start_session(
        {
            "session_id": session_id,
            "started_at": started_at,
            "source": source,
            "capture_device": capture_device,
        },
        state.name,
    )


async def apply_session_end(state: WorkerState, token: object, message: dict[str, Any]) -> None:
    try:
        session_id = require_session_id(message.get("session_id"))
    except ValueError:
        return
    ended_at = finite_timestamp(message.get("ended_at"))
    async with state.lock:
        if state.token is not token:
            return
        if state.active_session_id == session_id:
            state.live = False
            state.frame = None
            state.audio_packets.clear()
            state.last_audio_at = None
            state.active_session_id = None
            state.session_started_at = None
            state.transcripts.clear()
            state.transcript_fingerprints.clear()
        state.last_seen_at = time.time()
    await archive.end_session(session_id, ended_at)


async def apply_frame(state: WorkerState, token: object, frame: bytes) -> None:
    if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
        return
    async with state.lock:
        if state.token is not token:
            return
        state.last_seen_at = time.time()
        state.frame = frame
        state.frame_sequence += 1
        state.last_frame_at = time.time()


async def apply_live_audio(state: WorkerState, token: object, pcm: bytes) -> None:
    """Keep a short PCM ring buffer for viewers connected to this live worker."""
    if not pcm or len(pcm) > MAX_LIVE_AUDIO_PACKET_BYTES or len(pcm) % 2:
        return
    async with state.lock:
        if state.token is not token:
            return
        now = time.time()
        state.last_seen_at = now
        state.audio_sequence += 1
        state.audio_packets.append((state.audio_sequence, pcm))
        state.last_audio_at = now


async def apply_transcript(state: WorkerState, token: object, raw_line: dict[str, Any]) -> None:
    text = " ".join(str(raw_line.get("text", "")).split())
    if not text:
        return
    started = max(0.0, finite_timestamp(raw_line.get("started"), 0.0))
    line = {
        "id": str(raw_line.get("id") or f"{started:.2f}:{text}"),
        "text": text[:1_000],
        "started": round(started, 2),
        "received_at": time.time(),
    }
    fingerprint = f"{line['id']}:{line['text']}"
    session_id: str | None = None
    async with state.lock:
        if state.token is not token or fingerprint in state.transcript_fingerprints or not state.active_session_id:
            return
        state.last_seen_at = time.time()
        state.transcripts.append(line)
        state.transcript_fingerprints.append(fingerprint)
        session_id = state.active_session_id
    await archive.save_transcript(session_id, line)


def parse_archive_segment(payload: bytes) -> tuple[dict[str, Any], bytes] | None:
    if len(payload) < 12 or not payload.startswith(ARCHIVE_MAGIC):
        return None
    header_size = int.from_bytes(payload[4:8], "big")
    if header_size <= 0 or header_size > 8_192 or len(payload) <= 8 + header_size:
        return None
    try:
        metadata = json.loads(payload[8:8 + header_size])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("type") != "archive_segment":
        return None
    return metadata, payload[8 + header_size:]


def parse_archive_recording_chunk(payload: bytes) -> tuple[dict[str, Any], bytes] | None:
    if len(payload) < 12 or not payload.startswith(ARCHIVE_RECORDING_MAGIC):
        return None
    header_size = int.from_bytes(payload[4:8], "big")
    if header_size <= 0 or header_size > 8_192 or len(payload) <= 8 + header_size:
        return None
    try:
        metadata = json.loads(payload[8:8 + header_size])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("type") != "archive_recording_chunk":
        return None
    return metadata, payload[8 + header_size:]


def parse_archive_original_recording_chunk(payload: bytes) -> tuple[dict[str, Any], bytes] | None:
    """Decode a retained, unmastered camera recording byte range."""
    if len(payload) < 12 or not payload.startswith(ARCHIVE_ORIGINAL_RECORDING_MAGIC):
        return None
    header_size = int.from_bytes(payload[4:8], "big")
    if header_size <= 0 or header_size > 8_192 or len(payload) <= 8 + header_size:
        return None
    try:
        metadata = json.loads(payload[8:8 + header_size])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("type") != "archive_original_recording_chunk":
        return None
    return metadata, payload[8 + header_size:]


def parse_live_audio(payload: bytes) -> bytes | None:
    """Decode the compact, fixed-format live PCM publisher message."""
    if not payload.startswith(LIVE_AUDIO_MAGIC):
        return None
    pcm = payload[len(LIVE_AUDIO_MAGIC):]
    if not pcm or len(pcm) > MAX_LIVE_AUDIO_PACKET_BYTES or len(pcm) % 2:
        return None
    return pcm


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")


@app.get("/archive")
@app.get("/analytics")
async def linked_workspace_view() -> FileResponse:
    """Serve the single-page viewer for a shareable workspace tab URL."""
    return FileResponse(STATIC / "index.html", media_type="text/html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    if archive.pool is not None:
        return {"status": "ok", "archive": "ready"}
    if archive.database_required:
        # Keep the web process live so it can retry DNS/database recovery; the
        # archive endpoints themselves return 503 rather than false emptiness.
        return {"status": "degraded", "archive": "reconnecting"}
    return {"status": "ok", "archive": "local-development-fallback"}


@app.get("/api/workers")
async def list_workers() -> dict[str, list[dict[str, Any]]]:
    async with workers_lock:
        current = list(workers.values())
    online = [worker_payload(state) for state in current if state.is_streaming()]
    return {"workers": sorted(online, key=lambda item: str(item["worker"]).casefold())}


@app.get("/api/worker/{worker_name}")
async def worker_status(worker_name: str) -> JSONResponse:
    try:
        _, friendly_name = require_worker_name(worker_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = await existing_worker(worker_name)
    payload = worker_payload(state)
    payload["worker"] = friendly_name if state is None else state.name
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/worker/{worker_name}/transcript")
async def worker_transcript(worker_name: str) -> JSONResponse:
    try:
        _, friendly_name = require_worker_name(worker_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = await existing_worker(worker_name)
    if state is None or not state.is_streaming():
        return JSONResponse({"worker": friendly_name, "session_id": None, "live": False, "lines": []}, headers={"Cache-Control": "no-store"})
    async with state.lock:
        return JSONResponse(
            {"worker": state.name, "session_id": state.active_session_id, "live": state.is_streaming(), "lines": list(state.transcripts)},
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/worker/{worker_name}/archives")
async def worker_archives(worker_name: str) -> JSONResponse:
    try:
        _, friendly_name = require_worker_name(worker_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        sessions = await archive.list_sessions(friendly_name)
    except ArchiveUnavailable as exc:
        # The public client keeps its previous list and retries.  A 503 is
        # deliberate: an empty array would incorrectly look like an archive
        # deletion during a transient database or Render connection failure.
        raise HTTPException(
            status_code=503,
            detail="archive is temporarily unavailable; retry shortly",
            headers={"Retry-After": "3"},
        ) from exc
    return JSONResponse({"worker": friendly_name, "sessions": sessions}, headers={"Cache-Control": "no-store"})


@app.get("/api/worker/{worker_name}/analytics")
async def worker_analytics(worker_name: str) -> JSONResponse:
    try:
        _, friendly_name = require_worker_name(worker_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        sessions = await archive.list_sessions(friendly_name)
    except ArchiveUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="archive is temporarily unavailable; retry shortly",
            headers={"Retry-After": "3"},
        ) from exc
    total_seconds = sum(float(item["duration_seconds"]) for item in sessions)
    total_bytes = sum(int(item["size_bytes"]) for item in sessions)
    transcript_lines = sum(int(item["transcript_count"]) for item in sessions)
    live_state = await existing_worker(friendly_name)
    live_seconds = 0.0
    if live_state is not None and live_state.is_streaming() and live_state.session_started_at:
        live_seconds = max(0.0, time.time() - live_state.session_started_at)
    battery_capacity_minutes = 180.0
    battery_used_minutes = min(battery_capacity_minutes, (total_seconds + live_seconds) / 60.0)
    return JSONResponse({
        "worker": friendly_name,
        "recording_seconds": round(total_seconds + live_seconds, 1),
        "archived_bytes": total_bytes,
        "archive_sessions": len(sessions),
        "transcript_lines": transcript_lines,
        "battery": {
            "capacity_minutes": battery_capacity_minutes,
            "used_minutes": round(battery_used_minutes, 1),
            "remaining_minutes": round(max(0.0, battery_capacity_minutes - battery_used_minutes), 1),
            "remaining_percent": round(max(0.0, 100 * (1 - battery_used_minutes / battery_capacity_minutes)), 1),
        },
        "device": {"weight_g": 93, "neck_load_newtons": 0.91, "ergonomic_score": 76},
        "frontier": [
            {"label": "Lightweight", "runtime_minutes": 120, "weight_g": 74, "ergonomic_score": 84},
            {"label": "Reference camera", "runtime_minutes": 180, "weight_g": 93, "ergonomic_score": 76},
            {"label": "Extended runtime", "runtime_minutes": 240, "weight_g": 112, "ergonomic_score": 69},
        ],
        "note": "Device trade-off points are planning estimates; archive and transcription totals are measured from saved sessions.",
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/archive/{session_id}")
async def archive_detail(session_id: str) -> JSONResponse:
    try:
        detail = await archive.session_detail(session_id)
    except ArchiveUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="archive is temporarily unavailable; retry shortly",
            headers={"Retry-After": "3"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="archive session not found")
    return JSONResponse(detail, headers={"Cache-Control": "no-store"})


@app.get("/api/archive/{session_id}/analytics")
async def archive_analytics(session_id: str) -> JSONResponse:
    try:
        payload = await archive.analytics(session_id)
    except ArchiveUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="archive is temporarily unavailable; retry shortly",
            headers={"Retry-After": "3"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="video analytics are not ready")
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/archive/{session_id}/segment/{sequence}.mp4")
async def archive_segment(session_id: str, sequence: int) -> Response:
    try:
        segment = await archive.segment(session_id, sequence)
    except ArchiveUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="archive is temporarily unavailable; retry shortly",
            headers={"Retry-After": "3"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if segment is None:
        raise HTTPException(status_code=404, detail="archive segment not found")
    return Response(content=segment["data"], media_type=segment.get("content_type", "video/mp4"), headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def archive_recording_response(
    session_id: str, request: Request, *, original: bool = False, prefer_original: bool = False
) -> Response:
    try:
        if original:
            recording = await archive.original_recording(session_id)
        elif prefer_original:
            # New sessions upload an unmodified copy as their public archive.
            # Existing sessions may only have the older primary object, so
            # retain that as a playback fallback instead of returning a 404.
            recording = await archive.original_recording(session_id)
            if recording is None:
                recording = await archive.recording(session_id)
        else:
            recording = await archive.recording(session_id)
    except ArchiveUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="archive is temporarily unavailable; retry shortly",
            headers={"Retry-After": "3"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if recording is None:
        detail = "original camera recording is not ready" if original else "stitched archive recording is not ready"
        raise HTTPException(status_code=404, detail=detail)
    data = recording["data"]
    total = len(data)
    # A recording can be upgraded from playable parts to a stitched
    # final MP4 under the same stable archive URL. Do not let a browser retain
    # an older byte range indefinitely after that atomic replacement.
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
    if match:
        try:
            start = int(match.group(1)) if match.group(1) else 0
            end = int(match.group(2)) if match.group(2) else total - 1
        except ValueError:
            start, end = 0, total - 1
        if start >= total or start > end:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"})
        end = min(end, total - 1)
        payload = data[start:end + 1]
        return Response(
            content=payload,
            status_code=206,
            media_type=recording.get("content_type", "video/mp4"),
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{total}", "Content-Length": str(len(payload))},
        )
    return Response(
        content=data,
        media_type=recording.get("content_type", "video/mp4"),
        headers={**headers, "Content-Length": str(total)},
    )


@app.get("/archive/{session_id}/video.mp4")
async def archive_recording(session_id: str, request: Request) -> Response:
    """Preferred unmodified camera MP4, with a legacy playback fallback."""
    return await archive_recording_response(session_id, request, prefer_original=True)


@app.get("/archive/{session_id}/original.mp4")
async def archive_original_recording(session_id: str, request: Request) -> Response:
    """Unmodified camera MP4 retained for compatibility."""
    return await archive_recording_response(session_id, request, original=True)


@app.get("/stream/{worker_name}.mjpg")
async def worker_stream(worker_name: str) -> StreamingResponse:
    try:
        require_worker_name(worker_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def stream() -> AsyncIterator[bytes]:
        boundary = b"egocaptureframe"
        sent_sequence = -1
        while True:
            state = await existing_worker(worker_name)
            if state is not None:
                async with state.lock:
                    sequence = state.frame_sequence
                    frame = state.frame if state.is_streaming() else None
                if frame is not None and sequence != sent_sequence:
                    sent_sequence = sequence
                    yield b"--" + boundary + b"\r\nContent-Type: image/jpeg\r\n" + f"Content-Length: {len(frame)}\r\n\r\n".encode() + frame + b"\r\n"
            yield b"\r\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=egocaptureframe", headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@app.websocket("/ws/live-audio/{worker_name}")
async def stream_live_audio(websocket: WebSocket, worker_name: str) -> None:
    """Send body-camera PCM to a public viewer over a browser WebSocket.

    Each binary message is a four-byte big-endian relay sequence followed by
    16 kHz mono s16le PCM. The sequence is useful to clients that want to
    detect gaps, while keeping the audio payload independently decodable.
    """
    try:
        require_worker_name(worker_name)
    except ValueError:
        await websocket.close(code=1008, reason="invalid worker name")
        return
    await websocket.accept()
    sent_sequence = -1
    last_keepalive = time.monotonic()
    try:
        while True:
            state = await existing_worker(worker_name)
            packets: list[tuple[int, bytes]] = []
            if state is not None:
                async with state.lock:
                    if state.is_streaming():
                        if sent_sequence < 0:
                            # A viewer joining mid-stream gets just enough
                            # history to fill its jitter buffer, never seconds
                            # of old speech.
                            packets = list(state.audio_packets)[-8:]
                        else:
                            packets = [
                                item for item in state.audio_packets
                                if item[0] > sent_sequence
                            ]
            for sequence, pcm in packets:
                await websocket.send_bytes(sequence.to_bytes(4, "big") + pcm)
                sent_sequence = sequence
            now = time.monotonic()
            if not packets and now - last_keepalive >= 10:
                # Writes make an idle browser disconnect observable even when
                # the camera is silent. The page ignores text keepalives.
                await websocket.send_text("keepalive")
                last_keepalive = now
            await asyncio.sleep(0.04 if packets else 0.2)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/worker/{worker_name}")
async def publish_worker(websocket: WebSocket, worker_name: str) -> None:
    try:
        _, display_name = require_worker_name(worker_name)
    except ValueError:
        await websocket.close(code=1008, reason="invalid worker name")
        return
    await websocket.accept()
    _, state = await worker_for_publisher(display_name)
    token = object()
    async with state.lock:
        state.name = display_name
        state.token = token
        state.connected_at = time.time()
        state.last_seen_at = time.time()
        state.live = False
        state.frame = None
        state.audio_packets.clear()
        state.last_audio_at = None

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            payload_bytes = message.get("bytes")
            if payload_bytes is not None:
                parsed_archive = parse_archive_segment(payload_bytes)
                if parsed_archive is not None:
                    metadata, segment_data = parsed_archive
                    try:
                        await archive.save_segment(metadata, segment_data)
                    except (ValueError, TypeError):
                        pass
                else:
                    parsed_recording = parse_archive_recording_chunk(payload_bytes)
                    if parsed_recording is not None:
                        metadata, recording_data = parsed_recording
                        try:
                            await archive.save_recording_chunk(metadata, recording_data)
                        except (ValueError, TypeError):
                            pass
                    else:
                        parsed_original_recording = parse_archive_original_recording_chunk(payload_bytes)
                        if parsed_original_recording is not None:
                            metadata, recording_data = parsed_original_recording
                            try:
                                await archive.save_original_recording_chunk(metadata, recording_data)
                            except (ValueError, TypeError):
                                pass
                        elif len(payload_bytes) <= MAX_FRAME_BYTES:
                            parsed_audio = parse_live_audio(payload_bytes)
                            if parsed_audio is not None:
                                await apply_live_audio(state, token, parsed_audio)
                            else:
                                await apply_frame(state, token, payload_bytes)
                continue
            raw_text = message.get("text")
            if not raw_text:
                continue
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if kind == "status":
                await apply_status(state, token, payload)
            elif kind == "session_start":
                await apply_session_start(state, token, payload)
            elif kind == "session_end":
                await apply_session_end(state, token, payload)
            elif kind == "archive_session" and isinstance(payload.get("session"), dict):
                try:
                    await archive.start_session(payload["session"], state.name)
                except ValueError:
                    pass
            elif kind == "archive_transcript_replace" and isinstance(payload.get("lines"), list):
                try:
                    lines = [line for line in payload["lines"] if isinstance(line, dict)]
                    await archive.replace_transcript(require_session_id(payload.get("session_id")), lines)
                except ValueError:
                    pass
            elif kind == "archive_analytics" and isinstance(payload.get("analytics"), dict):
                try:
                    await archive.save_analytics(
                        require_session_id(payload.get("session_id")), payload["analytics"]
                    )
                except ValueError:
                    pass
            elif kind == "archive_delete":
                try:
                    await archive.delete_session(require_session_id(payload.get("session_id")))
                except ValueError:
                    pass
            elif kind == "archive_recording_complete":
                try:
                    await archive.complete_recording(
                        require_session_id(payload.get("session_id")),
                        int(payload.get("chunk_count", 0)),
                        int(payload.get("size_bytes", 0)),
                    )
                except (TypeError, ValueError):
                    pass
            elif kind == "archive_original_recording_complete":
                try:
                    await archive.complete_original_recording(
                        require_session_id(payload.get("session_id")),
                        int(payload.get("chunk_count", 0)),
                        int(payload.get("size_bytes", 0)),
                    )
                except (TypeError, ValueError):
                    pass
            elif kind in {"transcript", "archive_transcript"} and isinstance(payload.get("line"), dict):
                if kind == "transcript":
                    await apply_transcript(state, token, payload["line"])
                else:
                    try:
                        await archive.save_transcript(require_session_id(payload.get("session_id")), payload["line"])
                    except ValueError:
                        pass
    except WebSocketDisconnect:
        pass
    finally:
        session_id: str | None = None
        async with state.lock:
            if state.token is token:
                session_id = state.active_session_id
                state.live = False
                state.frame = None
                state.audio_packets.clear()
                state.last_audio_at = None
                state.last_seen_at = time.time()
                state.token = None
        if session_id:
            await archive.end_session(session_id)
