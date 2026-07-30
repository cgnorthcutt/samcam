"""Small public relay for Sam Cam's local, USB-only capture server.

The camera still belongs to a laptop running ``server.py``.  That laptop opens
one outbound WebSocket here and sends only current JPEG frames plus transcript
events.  Browser viewers receive an MJPEG stream from this service, which makes
the live view work through NAT without exposing the laptop to the internet.

This is intentionally a demo relay: it has no authentication because the demo
is explicitly public.  Do not use it for private or production footage.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
MAX_FRAME_BYTES = 5_000_000
FRAME_FRESH_SECONDS = 5.0
PUBLISHER_FRESH_SECONDS = 8.0
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")


def require_worker_name(raw_name: str) -> tuple[str, str]:
    """Return a stable key and a human-readable worker name."""
    name = raw_name.strip()
    if not WORKER_NAME.fullmatch(name):
        raise ValueError("worker name must be 1-63 letters, numbers, spaces, . _ or -")
    return name.casefold(), name


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
    transcripts: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    transcript_fingerprints: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    token: object | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_streaming(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(
            self.live
            and self.frame is not None
            and self.last_frame_at is not None
            and now - self.last_frame_at < FRAME_FRESH_SECONDS
            and self.last_seen_at is not None
            and now - self.last_seen_at < PUBLISHER_FRESH_SECONDS
        )


app = FastAPI(title="Sam Cam Relay", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
workers: dict[str, WorkerState] = {}
workers_lock = asyncio.Lock()


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
        return {
            "worker": None,
            "streaming": False,
            "last_frame_at": None,
            "source": None,
            "transcript_count": 0,
        }
    return {
        "worker": state.name,
        "streaming": state.is_streaming(),
        "last_frame_at": state.last_frame_at,
        "source": state.source,
        "connected_at": state.connected_at,
        "transcript_count": len(state.transcripts),
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
            # Never keep a last frame around for a disconnected/non-live worker.
            state.frame = None


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


async def apply_transcript(
    state: WorkerState, token: object, raw_line: dict[str, Any]
) -> None:
    text = " ".join(str(raw_line.get("text", "")).split())
    if not text:
        return
    try:
        started = max(0.0, float(raw_line.get("started", 0)))
    except (TypeError, ValueError):
        started = 0.0
    line = {"text": text[:1_000], "started": round(started, 2), "received_at": time.time()}
    fingerprint = f"{line['started']}:{line['text']}"
    async with state.lock:
        if state.token is not token or fingerprint in state.transcript_fingerprints:
            return
        state.last_seen_at = time.time()
        state.transcripts.append(line)
        state.transcript_fingerprints.append(fingerprint)


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
    lines = list(state.transcripts) if state is not None else []
    return JSONResponse(
        {"worker": friendly_name if state is None else state.name, "lines": lines},
        headers={"Cache-Control": "no-store"},
    )


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
                    yield (
                        b"--" + boundary + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                        + frame
                        + b"\r\n"
                    )
            # A periodic write keeps the connection observable without inventing
            # a frame; browsers continue showing the status supplied by the API.
            yield b"\r\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(
        stream(),
        media_type="multipart/x-mixed-replace; boundary=samcamframe",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


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
                if len(payload_bytes) <= MAX_FRAME_BYTES:
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
            elif kind == "transcript" and isinstance(payload.get("line"), dict):
                await apply_transcript(state, token, payload["line"])
    except WebSocketDisconnect:
        pass
    finally:
        async with state.lock:
            if state.token is token:
                state.live = False
                state.frame = None
                state.last_seen_at = time.time()
                state.token = None
