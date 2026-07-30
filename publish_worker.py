#!/usr/bin/env python3
"""Publish a local Sam Cam feed and create a durable, session-based archive.

This runs beside ``server.py`` on the laptop attached to the USB body camera.
It never accepts inbound traffic: frames, transcript lines, and compact MP4
segments travel over one outbound WebSocket to the public relay.  A local copy
of every session is retained under ``archives/`` as a recovery source.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

HERE = Path(__file__).resolve().parent
LOCAL_URL = os.environ.get("SAMCAM_LOCAL_URL", "http://127.0.0.1:8011").rstrip("/")
# Workers use Render directly for their long-lived WebSocket. Viewers use samcam.app.
RELAY_URL = os.environ.get("SAMCAM_RELAY_URL", "https://samcam-relay.onrender.com").rstrip("/")
WORKER = os.environ.get("SAMCAM_WORKER", "Curtis").strip() or "Curtis"
ARCHIVE_ROOT = Path(os.environ.get("SAMCAM_ARCHIVE_DIR", str(HERE / "archives"))).expanduser()
ARCHIVE_SEGMENT_SECONDS = max(5.0, float(os.environ.get("SAMCAM_ARCHIVE_SEGMENT_SECONDS", "10")))
ARCHIVE_FPS = max(1, int(os.environ.get("SAMCAM_ARCHIVE_FPS", "30")))
MAX_ARCHIVE_SEGMENT_BYTES = 8_000_000
RECONNECT_SECONDS = 2.0
ARCHIVE_MAGIC = b"SCAS"


@dataclass(frozen=True)
class LocalSegment:
    session_id: str
    sequence: int
    started_at: float
    duration_seconds: float
    path: Path


class SessionArchiver:
    """Write JPEGs into short files, encode them off the live-path, and retain them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.active: dict[str, Any] | None = None
        self.active_dir: Path | None = None
        self.active_raw: Any | None = None
        self.active_sequence = 0
        self.active_segment_started_at: float | None = None
        self.active_segment_frames = 0
        self.encoding: set[threading.Thread] = set()
        self.uploaded: set[tuple[str, int]] = set()
        self.errors: list[str] = []

    @staticmethod
    def _safe_worker(worker: str) -> str:
        return "".join(char if char.isalnum() else "-" for char in worker).strip("-")[:32] or "worker"

    @staticmethod
    def _metadata_path(session_dir: Path) -> Path:
        return session_dir / "metadata.json"

    @staticmethod
    def _transcript_path(session_dir: Path) -> Path:
        return session_dir / "transcript.jsonl"

    def _write_metadata(self, session_dir: Path, metadata: dict[str, Any]) -> None:
        target = self._metadata_path(session_dir)
        temporary = target.with_suffix(".json.part")
        temporary.write_text(json.dumps(metadata, separators=(",", ":"), sort_keys=True))
        temporary.replace(target)

    def _open_segment_locked(self, started_at: float) -> None:
        if self.active is None or self.active_dir is None:
            return
        raw_path = self.active_dir / f"segment-{self.active_sequence:05d}.mjpeg"
        self.active_raw = raw_path.open("wb")
        self.active_segment_started_at = started_at
        self.active_segment_frames = 0

    def _encode_segment(self, raw_path: Path, segment: LocalSegment) -> None:
        output = segment.path
        sidecar = output.with_suffix(".json")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            with self.lock:
                self.errors.append("ffmpeg is unavailable; archive segment was retained as MJPEG")
                del self.errors[:-10]
            return
        try:
            completed = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "image2pipe", "-framerate", str(ARCHIVE_FPS), "-c:v", "mjpeg", "-i", str(raw_path),
                    "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-crf", "28", "-maxrate", "1100k", "-bufsize", "1800k", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(output),
                ],
                capture_output=True,
                timeout=180,
            )
            if completed.returncode != 0 or not output.exists() or output.stat().st_size == 0:
                detail = completed.stderr.decode(errors="replace").strip()[-500:]
                raise RuntimeError(detail or "ffmpeg produced no MP4")
            sidecar.write_text(json.dumps({
                "session_id": segment.session_id,
                "sequence": segment.sequence,
                "started_at": segment.started_at,
                "duration_seconds": segment.duration_seconds,
            }, separators=(",", ":")))
            raw_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - live publishing must never stop for archive encoding
            with self.lock:
                self.errors.append(f"archive segment {segment.sequence}: {exc}"[-600:])
                del self.errors[:-10]

    def _finish_segment_locked(self, ended_at: float) -> None:
        if self.active is None or self.active_dir is None or self.active_raw is None or self.active_segment_started_at is None:
            return
        raw_path = Path(self.active_raw.name)
        self.active_raw.close()
        sequence = self.active_sequence
        started_at = self.active_segment_started_at
        frames = self.active_segment_frames
        self.active_raw = None
        self.active_segment_started_at = None
        self.active_segment_frames = 0
        self.active_sequence += 1
        if frames <= 0 or not raw_path.exists() or raw_path.stat().st_size == 0:
            raw_path.unlink(missing_ok=True)
            return
        segment = LocalSegment(
            session_id=self.active["session_id"],
            sequence=sequence,
            started_at=started_at,
            duration_seconds=round(max(0.1, ended_at - started_at), 2),
            path=self.active_dir / f"segment-{sequence:05d}.mp4",
        )

        def encode() -> None:
            self._encode_segment(raw_path, segment)

        thread = threading.Thread(target=encode, daemon=True, name=f"samcam-archive-{sequence}")
        self.encoding.add(thread)
        thread.start()

    def start(self, worker: str, source: str | None) -> dict[str, Any]:
        with self.lock:
            if self.active is not None:
                return dict(self.active)
            started_at = time.time()
            stamp = datetime.fromtimestamp(started_at, UTC).strftime("%Y%m%dT%H%M%SZ")
            session_id = f"{self._safe_worker(worker)}-{stamp}-{uuid.uuid4().hex[:8]}"
            session_dir = self.root / session_id
            session_dir.mkdir(parents=True, exist_ok=False)
            self.active = {
                "session_id": session_id,
                "worker_name": worker,
                "source": source,
                "started_at": started_at,
                "ended_at": None,
                "status": "recording",
            }
            self.active_dir = session_dir
            self.active_sequence = 0
            self._write_metadata(session_dir, self.active)
            self._open_segment_locked(started_at)
            return dict(self.active)

    def session(self) -> dict[str, Any] | None:
        with self.lock:
            return dict(self.active) if self.active is not None else None

    def write_frame(self, frame: bytes) -> None:
        with self.lock:
            if self.active_raw is None or self.active_segment_started_at is None:
                return
            now = time.time()
            if self.active_segment_frames and now - self.active_segment_started_at >= ARCHIVE_SEGMENT_SECONDS:
                self._finish_segment_locked(now)
                self._open_segment_locked(now)
            if self.active_raw is None:
                return
            self.active_raw.write(frame)
            self.active_segment_frames += 1

    def append_transcript(self, line: dict[str, Any]) -> None:
        with self.lock:
            if self.active is None or self.active_dir is None:
                return
            payload = {
                "id": str(line.get("id") or ""),
                "text": " ".join(str(line.get("text") or "").split())[:1_000],
                "started": float(line.get("started") or 0),
                "received_at": float(line.get("received_at") or time.time()),
            }
            if payload["text"]:
                with self._transcript_path(self.active_dir).open("a") as handle:
                    handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def stop(self) -> dict[str, Any] | None:
        with self.lock:
            if self.active is None or self.active_dir is None:
                return None
            ended_at = time.time()
            self._finish_segment_locked(ended_at)
            self.active["ended_at"] = ended_at
            self.active["status"] = "complete"
            self._write_metadata(self.active_dir, self.active)
            completed = dict(self.active)
            self.active = None
            self.active_dir = None
            return completed

    def reset_uploads(self) -> None:
        with self.lock:
            self.uploaded.clear()

    def manifests(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for metadata_path in self.root.glob("*/metadata.json"):
            try:
                payload = json.loads(metadata_path.read_text())
                if isinstance(payload, dict) and payload.get("session_id"):
                    entries.append(payload)
            except (OSError, ValueError):
                continue
        return sorted(entries, key=lambda item: float(item.get("started_at") or 0))

    def transcript_lines(self, session_id: str) -> list[dict[str, Any]]:
        transcript_path = self.root / session_id / "transcript.jsonl"
        if not transcript_path.exists():
            return []
        lines: list[dict[str, Any]] = []
        try:
            for raw in transcript_path.read_text().splitlines():
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("text"):
                    lines.append(payload)
        except (OSError, ValueError):
            return []
        return lines

    def next_segment(self) -> LocalSegment | None:
        for metadata in self.manifests():
            session_id = str(metadata["session_id"])
            session_dir = self.root / session_id
            for path in sorted(session_dir.glob("segment-*.mp4")):
                try:
                    sequence = int(path.stem.rsplit("-", 1)[1])
                    if (session_id, sequence) in self.uploaded:
                        continue
                    details = json.loads(path.with_suffix(".json").read_text())
                    return LocalSegment(
                        session_id=session_id,
                        sequence=sequence,
                        started_at=float(details["started_at"]),
                        duration_seconds=float(details["duration_seconds"]),
                        path=path,
                    )
                except (OSError, ValueError, KeyError):
                    continue
        return None

    def mark_uploaded(self, segment: LocalSegment) -> None:
        with self.lock:
            self.uploaded.add((segment.session_id, segment.sequence))


def relay_websocket_url() -> str:
    parsed = urlsplit(RELAY_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("SAMCAM_RELAY_URL must start with http:// or https://")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/ws/worker/{quote(WORKER)}", "", ""))


async def get_json(session: aiohttp.ClientSession, path: str) -> dict[str, Any] | None:
    try:
        async with session.get(f"{LOCAL_URL}{path}", timeout=aiohttp.ClientTimeout(total=2)) as response:
            if response.status != 200:
                return None
            payload = await response.json()
            return payload if isinstance(payload, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def transcript_high_watermark(payload: dict[str, Any] | None) -> int:
    values: list[int] = []
    for line in (payload or {}).get("lines", []):
        if isinstance(line, dict):
            with suppress(TypeError, ValueError):
                values.append(int(line.get("id", -1)))
    return max(values, default=-1)


async def sync_archive_index(archiver: SessionArchiver, send: Any) -> None:
    """Tell a newly connected relay about retained sessions and their text."""
    for metadata in archiver.manifests():
        await send({"type": "archive_session", "session": metadata})
        for line in archiver.transcript_lines(str(metadata["session_id"])):
            await send({"type": "archive_transcript", "session_id": metadata["session_id"], "line": line})


async def upload_next_segment(archiver: SessionArchiver, send: Any) -> None:
    segment = archiver.next_segment()
    if segment is None:
        return
    try:
        data = await asyncio.to_thread(segment.path.read_bytes)
    except OSError:
        return
    if not data or len(data) > MAX_ARCHIVE_SEGMENT_BYTES:
        print(f"  archive segment {segment.path.name} exceeds the {MAX_ARCHIVE_SEGMENT_BYTES // 1_000_000} MB relay limit", file=sys.stderr)
        archiver.mark_uploaded(segment)
        return
    metadata = json.dumps({
        "type": "archive_segment",
        "session_id": segment.session_id,
        "sequence": segment.sequence,
        "started_at": segment.started_at,
        "duration_seconds": segment.duration_seconds,
    }, separators=(",", ":")).encode()
    await send(ARCHIVE_MAGIC + len(metadata).to_bytes(4, "big") + metadata + data)
    archiver.mark_uploaded(segment)


async def publish_status(session: aiohttp.ClientSession, send: Any, stop: asyncio.Event, archiver: SessionArchiver) -> None:
    transcript_id = -1
    announced_session_id: str | None = None
    await sync_archive_index(archiver, send)
    while not stop.is_set():
        stream = await get_json(session, "/api/stream")
        fresh_at = stream.get("last_live_frame_at") if stream else None
        try:
            live = bool(stream and stream.get("live") and fresh_at and time.time() - float(fresh_at) < 5)
        except (TypeError, ValueError):
            live = False
        source = stream.get("source") if stream else None
        transcript = await get_json(session, "/api/transcript")
        active = archiver.session()
        if live and active is None:
            active = archiver.start(WORKER, str(source) if source else None)
            # The local transcriber retains recent lines.  Set the watermark at
            # session start so the public live panel never labels old speech as current.
            transcript_id = transcript_high_watermark(transcript)
        if live and active is not None and announced_session_id != active["session_id"]:
            # On a WebSocket reconnect, sync_archive_index has already replayed
            # this session's locally saved lines. Do not then re-label the
            # transcriber's older in-memory lines as new live speech.
            transcript_id = max(transcript_id, transcript_high_watermark(transcript))
            await send({"type": "session_start", "session": active})
            announced_session_id = str(active["session_id"])
        if not live and active is not None:
            completed = archiver.stop()
            if completed is not None:
                await send({"type": "session_end", "session_id": completed["session_id"], "ended_at": completed["ended_at"]})
            announced_session_id = None
            transcript_id = transcript_high_watermark(transcript)
        current_session = archiver.session()
        await send({"type": "status", "live": live, "source": source, "session_id": current_session.get("session_id") if current_session else None})
        if live and current_session is not None:
            for line in (transcript or {}).get("lines", []):
                if not isinstance(line, dict):
                    continue
                try:
                    line_id = int(line.get("id", -1))
                except (TypeError, ValueError):
                    continue
                if line_id > transcript_id:
                    transcript_id = line_id
                    event = dict(line)
                    event["received_at"] = time.time()
                    archiver.append_transcript(event)
                    await send({"type": "transcript", "line": event})
        await upload_next_segment(archiver, send)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def publish_frames(session: aiohttp.ClientSession, send: Any, stop: asyncio.Event, archiver: SessionArchiver) -> None:
    """Extract JPEG ranges from local multipart MJPEG output without adding latency."""
    while not stop.is_set():
        try:
            async with session.get(f"{LOCAL_URL}/stream.mjpg", timeout=aiohttp.ClientTimeout(total=None, sock_read=15)) as response:
                if response.status != 200:
                    raise RuntimeError(f"local stream returned HTTP {response.status}")
                buffer = bytearray()
                async for chunk in response.content.iter_any():
                    if stop.is_set():
                        return
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        if start < 0:
                            if len(buffer) > 1_000_000:
                                del buffer[:-2]
                            break
                        if start:
                            del buffer[:start]
                        end = buffer.find(b"\xff\xd9", 2)
                        if end < 0:
                            break
                        frame = bytes(buffer[:end + 2])
                        del buffer[:end + 2]
                        if len(frame) <= 5_000_000:
                            archiver.write_frame(frame)
                            await send(frame)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            print(f"  local stream: {exc}", file=sys.stderr)
            try:
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
            except asyncio.TimeoutError:
                pass


async def connected_publisher(stop: asyncio.Event, archiver: SessionArchiver) -> None:
    websocket_url = relay_websocket_url()
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(websocket_url, heartbeat=20, autoping=True, max_msg_size=10_000_000) as websocket:
                    print(f"  publishing {WORKER} to {RELAY_URL}")
                    archiver.reset_uploads()
                    send_lock = asyncio.Lock()

                    async def send(payload: dict[str, Any] | bytes) -> None:
                        async with send_lock:
                            if isinstance(payload, bytes):
                                await websocket.send_bytes(payload)
                            else:
                                await websocket.send_json(payload)

                    status_task = asyncio.create_task(publish_status(session, send, stop, archiver))
                    frames_task = asyncio.create_task(publish_frames(session, send, stop, archiver))
                    stop_task = asyncio.create_task(stop.wait())
                    done, pending = await asyncio.wait({status_task, frames_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        with suppress(asyncio.CancelledError):
                            await task
                    for task in done:
                        if task is not stop_task:
                            with suppress(asyncio.CancelledError):
                                task.result()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                print(f"  relay connection: {exc}; retrying", file=sys.stderr)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
                except asyncio.TimeoutError:
                    pass


async def main() -> None:
    stop = asyncio.Event()
    archiver = SessionArchiver(ARCHIVE_ROOT)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    print(f"Sam Cam public publisher\n  local: {LOCAL_URL}\n  relay: {RELAY_URL}\n  worker: {WORKER}\n  archive: {ARCHIVE_ROOT}")
    await connected_publisher(stop, archiver)
    archiver.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
