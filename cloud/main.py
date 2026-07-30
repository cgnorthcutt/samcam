"""Public Sam Cam relay, live transcript, and session archive.

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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

try:  # Allows local relay tests before its optional database dependency is installed.
    import asyncpg
except ImportError:  # pragma: no cover - exercised only by a minimal local install
    asyncpg = None  # type: ignore[assignment]

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
MAX_FRAME_BYTES = 5_000_000
MAX_ARCHIVE_SEGMENT_BYTES = 8_000_000
FRAME_FRESH_SECONDS = 5.0
PUBLISHER_FRESH_SECONDS = 8.0
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
ARCHIVE_MAGIC = b"SCAS"


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


class ArchiveStore:
    """Postgres-backed archive with an in-memory fallback for local development."""

    def __init__(self) -> None:
        self.pool: Any | None = None
        self.error: str | None = None
        self.memory_sessions: dict[str, dict[str, Any]] = {}
        self.memory_segments: dict[tuple[str, int], dict[str, Any]] = {}
        self.memory_transcripts: dict[tuple[str, str], dict[str, Any]] = {}

    async def start(self) -> None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            self.error = "archive database is not configured"
            return
        if asyncpg is None:
            self.error = "asyncpg is not installed"
            return
        try:
            self.pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3, command_timeout=30)
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS samcam_archive_sessions (
                        session_id TEXT PRIMARY KEY,
                        worker_name TEXT NOT NULL,
                        source TEXT,
                        started_at DOUBLE PRECISION NOT NULL,
                        ended_at DOUBLE PRECISION,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE TABLE IF NOT EXISTS samcam_archive_segments (
                        session_id TEXT NOT NULL REFERENCES samcam_archive_sessions(session_id) ON DELETE CASCADE,
                        sequence INTEGER NOT NULL,
                        started_at DOUBLE PRECISION NOT NULL,
                        duration_seconds DOUBLE PRECISION NOT NULL,
                        content_type TEXT NOT NULL,
                        data BYTEA NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        PRIMARY KEY (session_id, sequence)
                    );
                    CREATE TABLE IF NOT EXISTS samcam_archive_transcripts (
                        session_id TEXT NOT NULL REFERENCES samcam_archive_sessions(session_id) ON DELETE CASCADE,
                        line_key TEXT NOT NULL,
                        started_at DOUBLE PRECISION NOT NULL,
                        received_at DOUBLE PRECISION NOT NULL,
                        text TEXT NOT NULL,
                        PRIMARY KEY (session_id, line_key)
                    );
                    CREATE INDEX IF NOT EXISTS samcam_archive_sessions_worker_idx
                        ON samcam_archive_sessions (worker_name, started_at DESC);
                    CREATE INDEX IF NOT EXISTS samcam_archive_transcripts_session_idx
                        ON samcam_archive_transcripts (session_id, started_at);
                    """
                )
            self.error = None
        except Exception as exc:  # noqa: BLE001 - the live relay must survive an archive outage
            self.pool = None
            self.error = f"archive database unavailable: {exc}"[-300:]
            print(self.error, flush=True)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def start_session(self, session: dict[str, Any], worker_name: str) -> None:
        session_id = require_session_id(session.get("session_id"))
        record = {
            "session_id": session_id,
            "worker_name": worker_name,
            "source": str(session.get("source") or "")[:160] or None,
            "started_at": finite_timestamp(session.get("started_at")),
            "ended_at": finite_timestamp(session["ended_at"]) if session.get("ended_at") else None,
        }
        prior = self.memory_sessions.get(session_id)
        self.memory_sessions[session_id] = {**(prior or {}), **record, "ended_at": record["ended_at"] or (prior or {}).get("ended_at")}
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO samcam_archive_sessions (session_id, worker_name, source, started_at, ended_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (session_id) DO UPDATE SET
                      worker_name = EXCLUDED.worker_name,
                      source = COALESCE(EXCLUDED.source, samcam_archive_sessions.source),
                      started_at = LEAST(EXCLUDED.started_at, samcam_archive_sessions.started_at),
                      ended_at = COALESCE(EXCLUDED.ended_at, samcam_archive_sessions.ended_at)
                    """,
                    record["session_id"], record["worker_name"], record["source"], record["started_at"], record["ended_at"],
                )
        except Exception as exc:  # noqa: BLE001
            self.error = f"archive write failed: {exc}"[-300:]
            print(self.error, flush=True)

    async def end_session(self, session_id: str, ended_at: float | None = None) -> None:
        session_id = require_session_id(session_id)
        ended_at = finite_timestamp(ended_at)
        if session_id in self.memory_sessions:
            self.memory_sessions[session_id]["ended_at"] = ended_at
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    "UPDATE samcam_archive_sessions SET ended_at = $2 WHERE session_id = $1",
                    session_id, ended_at,
                )
        except Exception as exc:  # noqa: BLE001
            self.error = f"archive update failed: {exc}"[-300:]
            print(self.error, flush=True)

    async def save_segment(self, metadata: dict[str, Any], data: bytes) -> None:
        session_id = require_session_id(metadata.get("session_id"))
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
        self.memory_segments[(session_id, sequence)] = record
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO samcam_archive_segments
                      (session_id, sequence, started_at, duration_seconds, content_type, data, size_bytes)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (session_id, sequence) DO NOTHING
                    """,
                    record["session_id"], record["sequence"], record["started_at"], record["duration_seconds"],
                    record["content_type"], record["data"], record["size_bytes"],
                )
        except Exception as exc:  # noqa: BLE001
            self.error = f"archive segment failed: {exc}"[-300:]
            print(self.error, flush=True)

    async def save_transcript(self, session_id: str, line: dict[str, Any]) -> None:
        session_id = require_session_id(session_id)
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
        self.memory_transcripts[(session_id, line_key)] = record
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO samcam_archive_transcripts
                      (session_id, line_key, started_at, received_at, text)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (session_id, line_key) DO NOTHING
                    """,
                    record["session_id"], record["line_key"], record["started_at"], record["received_at"], record["text"],
                )
        except Exception as exc:  # noqa: BLE001
            self.error = f"archive transcript failed: {exc}"[-300:]
            print(self.error, flush=True)

    def _memory_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        session_id = record["session_id"]
        segments = [value for (candidate, _), value in self.memory_segments.items() if candidate == session_id]
        transcripts = [value for (candidate, _), value in self.memory_transcripts.items() if candidate == session_id]
        return {
            **record,
            "duration_seconds": session_duration(record),
            "segment_count": len(segments),
            "size_bytes": sum(int(item["size_bytes"]) for item in segments),
            "transcript_count": len(transcripts),
        }

    async def list_sessions(self, worker_name: str) -> list[dict[str, Any]]:
        if self.pool is None:
            return sorted(
                [self._memory_summary(record) for record in self.memory_sessions.values() if record["worker_name"].casefold() == worker_name.casefold()],
                key=lambda item: float(item["started_at"]), reverse=True,
            )
        try:
            async with self.pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT s.session_id, s.worker_name, s.source, s.started_at, s.ended_at,
                      COALESCE((SELECT COUNT(*) FROM samcam_archive_segments g WHERE g.session_id = s.session_id), 0) AS segment_count,
                      COALESCE((SELECT SUM(g.size_bytes) FROM samcam_archive_segments g WHERE g.session_id = s.session_id), 0) AS size_bytes,
                      COALESCE((SELECT COUNT(*) FROM samcam_archive_transcripts t WHERE t.session_id = s.session_id), 0) AS transcript_count
                    FROM samcam_archive_sessions s
                    WHERE LOWER(s.worker_name) = LOWER($1)
                    ORDER BY s.started_at DESC
                    LIMIT 100
                    """, worker_name,
                )
            sessions = [dict(row) for row in rows]
            for record in sessions:
                record["duration_seconds"] = session_duration(record)
            return sessions
        except Exception as exc:  # noqa: BLE001
            self.error = f"archive read failed: {exc}"[-300:]
            print(self.error, flush=True)
            return []

    async def session_detail(self, session_id: str) -> dict[str, Any] | None:
        session_id = require_session_id(session_id)
        if self.pool is None:
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
            async with self.pool.acquire() as connection:
                session = await connection.fetchrow(
                    "SELECT session_id, worker_name, source, started_at, ended_at FROM samcam_archive_sessions WHERE session_id = $1", session_id
                )
                if session is None:
                    return None
                segments = await connection.fetch(
                    "SELECT sequence, started_at, duration_seconds, size_bytes FROM samcam_archive_segments WHERE session_id = $1 ORDER BY sequence", session_id
                )
                transcript = await connection.fetch(
                    "SELECT text, started_at AS started, received_at FROM samcam_archive_transcripts WHERE session_id = $1 ORDER BY started_at", session_id
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
            self.error = f"archive detail failed: {exc}"[-300:]
            print(self.error, flush=True)
            return None

    async def segment(self, session_id: str, sequence: int) -> dict[str, Any] | None:
        session_id = require_session_id(session_id)
        if sequence < 0:
            return None
        if self.pool is None:
            return self.memory_segments.get((session_id, sequence))
        try:
            async with self.pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT content_type, data, size_bytes FROM samcam_archive_segments WHERE session_id = $1 AND sequence = $2",
                    session_id, sequence,
                )
            return dict(row) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            self.error = f"archive segment read failed: {exc}"[-300:]
            print(self.error, flush=True)
            return None


@dataclass
class WorkerState:
    name: str
    live: bool = False
    source: str | None = None
    frame: bytes | None = None
    frame_sequence: int = 0
    last_frame_at: float | None = None
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
    yield
    await archive.close()


app = FastAPI(title="Sam Cam Relay", docs_url=None, redoc_url=None, lifespan=lifespan)
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
        return {"worker": None, "streaming": False, "last_frame_at": None, "source": None, "session_id": None, "transcript_count": 0}
    return {
        "worker": state.name,
        "streaming": state.is_streaming(),
        "last_frame_at": state.last_frame_at,
        "source": state.source,
        "connected_at": state.connected_at,
        "session_id": state.active_session_id if state.is_streaming() else None,
        "session_started_at": state.session_started_at if state.is_streaming() else None,
        "transcript_count": len(state.transcripts) if state.is_streaming() else 0,
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


async def apply_session_start(state: WorkerState, token: object, message: dict[str, Any]) -> None:
    raw_session = message.get("session") if isinstance(message.get("session"), dict) else message
    try:
        session_id = require_session_id(raw_session.get("session_id"))
    except (AttributeError, ValueError):
        return
    started_at = finite_timestamp(raw_session.get("started_at"))
    source = str(raw_session.get("source") or "")[:160] or None
    async with state.lock:
        if state.token is not token:
            return
        state.active_session_id = session_id
        state.session_started_at = started_at
        state.transcripts.clear()
        state.transcript_fingerprints.clear()
        if source:
            state.source = source
        state.last_seen_at = time.time()
    await archive.start_session({"session_id": session_id, "started_at": started_at, "source": source}, state.name)


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


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "archive": "ready" if archive.pool is not None else "local-fallback"}


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
    return JSONResponse({"worker": friendly_name, "sessions": await archive.list_sessions(friendly_name)}, headers={"Cache-Control": "no-store"})


@app.get("/api/worker/{worker_name}/analytics")
async def worker_analytics(worker_name: str) -> JSONResponse:
    try:
        _, friendly_name = require_worker_name(worker_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sessions = await archive.list_sessions(friendly_name)
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
            {"label": "Sam Cam", "runtime_minutes": 180, "weight_g": 93, "ergonomic_score": 76},
            {"label": "Extended runtime", "runtime_minutes": 240, "weight_g": 112, "ergonomic_score": 69},
        ],
        "note": "Device trade-off points are planning estimates; archive and transcription totals are measured from saved sessions.",
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/archive/{session_id}")
async def archive_detail(session_id: str) -> JSONResponse:
    try:
        detail = await archive.session_detail(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="archive session not found")
    return JSONResponse(detail, headers={"Cache-Control": "no-store"})


@app.get("/archive/{session_id}/segment/{sequence}.mp4")
async def archive_segment(session_id: str, sequence: int) -> Response:
    try:
        segment = await archive.segment(session_id, sequence)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if segment is None:
        raise HTTPException(status_code=404, detail="archive segment not found")
    return Response(content=segment["data"], media_type=segment.get("content_type", "video/mp4"), headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/stream/{worker_name}.mjpg")
async def worker_stream(worker_name: str) -> StreamingResponse:
    try:
        require_worker_name(worker_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def stream() -> AsyncIterator[bytes]:
        boundary = b"samcamframe"
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

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=samcamframe", headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


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
                elif len(payload_bytes) <= MAX_FRAME_BYTES:
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
                state.last_seen_at = time.time()
                state.token = None
        if session_id:
            await archive.end_session(session_id)
