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
# Parts are a recovery/preview format.  Five minutes keeps the archive usable
# without turning every short session into dozens of tiny videos.  Operators
# can still request shorter parts when a relay or network needs them.
ARCHIVE_SEGMENT_SECONDS = max(5.0, float(os.environ.get("SAMCAM_ARCHIVE_SEGMENT_SECONDS", "300")))
ARCHIVE_FPS = max(1, int(os.environ.get("SAMCAM_ARCHIVE_FPS", "30")))
MAX_ARCHIVE_SEGMENT_BYTES = max(500_000, int(os.environ.get("SAMCAM_ARCHIVE_SEGMENT_MAX_BYTES", "8000000")))
# Keep each preview part below the relay's single-WebSocket-message limit.  A
# five minute part needs a much lower bitrate than a ten second part, while
# the browser's live MJPEG feed remains entirely unaffected.
ARCHIVE_VIDEO_MAX_KBPS = max(64, int(os.environ.get("SAMCAM_ARCHIVE_VIDEO_MAX_KBPS", "1100")))
ARCHIVE_SEGMENT_SIZE_HEADROOM = 0.80
ARCHIVE_RECORDING_CHUNK_BYTES = 1_500_000
ARCHIVE_RECORDING_CHUNKS_PER_TICK = max(
    1, int(os.environ.get("SAMCAM_ARCHIVE_RECORDING_CHUNKS_PER_TICK", "3"))
)
RECONNECT_SECONDS = 2.0
# A faster status heartbeat starts the public relay as soon as the laptop has
# its first camera frame. Frame publishing itself remains continuous.
STATUS_POLL_SECONDS = 0.5
# The native camera helper produces 16 kHz mono signed 16-bit PCM. Prefixing
# each WebSocket payload keeps it distinct from JPEG frames and the two archive
# message formats while preserving a low-latency, codec-free live path.
LIVE_AUDIO_MAGIC = b"SCAU"
LIVE_AUDIO_FRAME_BYTES = 1_600  # 50 ms at 16 kHz mono s16le
ARCHIVE_MAGIC = b"SCAS"
ARCHIVE_RECORDING_MAGIC = b"SCAR"
ANALYTICS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LocalSegment:
    session_id: str
    sequence: int
    started_at: float
    duration_seconds: float
    path: Path
    frame_count: int = 0


@dataclass(frozen=True)
class LocalRecordingChunk:
    session_id: str
    path: Path
    index: int
    count: int
    size_bytes: int


def archive_preview_bitrate_kbps(duration_seconds: float) -> int:
    """Choose a bounded preview bitrate that keeps one part relay-safe.

    The public relay deliberately caps a binary archive part below its WebSocket
    message limit.  The local/full recording stays at normal archive quality;
    only its public recovery preview is constrained to this limit.
    """
    seconds = max(1.0, duration_seconds)
    relay_limit_kbps = int(
        MAX_ARCHIVE_SEGMENT_BYTES * 8 * ARCHIVE_SEGMENT_SIZE_HEADROOM / seconds / 1_000
    )
    return max(64, min(ARCHIVE_VIDEO_MAX_KBPS, relay_limit_kbps))


def analyze_recording(path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Create the same lightweight, video-derived product signals as the local UI.

    This stays on the laptop that holds the camera/video file.  The public
    relay receives the compact JSON result, never the raw sampled frames.
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise RuntimeError("ffmpeg and ffprobe are required for archive analytics")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        timeout=60,
    )
    duration = float(probe.stdout.decode(errors="replace").strip())
    if duration <= 0:
        raise RuntimeError("stitched recording has no measurable duration")

    # At most 180 grayscale frames: enough to preserve the profile while
    # keeping analysis quick for both a short demo and a long field session.
    sample_fps = min(1.0, max(0.05, 180.0 / duration))
    width, height = 64, 36
    frame_bytes = width * height
    sampled = subprocess.run(
        [
            ffmpeg, "-v", "error", "-i", str(path),
            "-vf", f"fps={sample_fps:.6f},scale={width}:{height}:flags=area:out_range=full,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
        ],
        capture_output=True,
        timeout=240,
    )
    if sampled.returncode != 0:
        raise RuntimeError(sampled.stderr.decode(errors="replace").strip()[-300:] or "ffmpeg could not sample video")
    raw = sampled.stdout
    frame_count = len(raw) // frame_bytes
    if frame_count == 0:
        raise RuntimeError("ffmpeg returned no analyzable frames")

    samples: list[dict[str, float]] = []
    previous: bytes | None = None
    for index in range(frame_count):
        frame = raw[index * frame_bytes:(index + 1) * frame_bytes]
        luminance = sum(frame) / (frame_bytes * 255.0) * 100.0
        if previous is None:
            motion = 0.0
        else:
            mean_delta = sum(abs(current - prior) for current, prior in zip(frame, previous)) / frame_bytes
            motion = min(100.0, mean_delta / 255.0 * 200.0 / (1.0 / sample_fps))
        previous = frame
        lighting = max(0.0, 100.0 - abs(luminance - 50.0) * 2.2)
        stability = max(0.0, 100.0 - motion)
        quality = lighting * 0.55 + stability * 0.45
        samples.append({
            "time": round(min(duration, (index + 0.5) / sample_fps), 2),
            "motion": round(motion, 1),
            "luminance": round(luminance, 1),
            "lighting": round(lighting, 1),
            "stability": round(stability, 1),
            "quality": round(quality, 1),
        })

    motion_samples = samples[1:] if len(samples) > 1 else samples
    motions = sorted(sample["motion"] for sample in motion_samples)
    p95_index = min(len(motions) - 1, int(len(motions) * 0.95))
    return {
        "schema_version": ANALYTICS_SCHEMA_VERSION,
        "generated_at": time.time(),
        "session_id": str(metadata["session_id"]),
        "clip": {
            "id": str(metadata["session_id"]),
            "name": str(metadata.get("source") or "Sam Cam recording"),
            "duration": round(duration, 2),
        },
        "sample_fps": round(sample_fps, 4),
        "samples": samples,
        "summary": {
            "average_motion": round(sum(sample["motion"] for sample in motion_samples) / len(motion_samples), 1),
            "peak_motion_p95": motions[p95_index],
            "average_lighting": round(sum(sample["lighting"] for sample in samples) / len(samples), 1),
            "average_quality": round(sum(sample["quality"] for sample in samples) / len(samples), 1),
            "stable_share": round(100.0 * sum(sample["motion"] < 25 for sample in motion_samples) / len(motion_samples), 1),
        },
        "device": {
            "asin": "B08KY7KLPB",
            "weight_grams": 89.9,
            "battery_capacity_mah": 800,
            "nominal_runtime_minutes": 270,
            "claimed_runtime_range_minutes": [240, 360],
            "storage_gb": 128,
            "field_of_view_degrees": 90,
        },
        "method": {
            "video_derived": ["video duration", "sampled frame luminance", "sampled frame-to-frame motion"],
            "model_assumptions": ["lighting quality favors mid-range luminance", "capture index weights lighting 55% and stability 45%"],
            "estimated": ["battery remaining and ETA", "effective worn load and neck torque", "ergonomic and market-fit scores"],
            "limitations": ["battery values are listing-based estimates, not telemetry", "motion is a frame-difference index, not physical acceleration", "ergonomic and suitability scores are unvalidated scenario models"],
        },
    }


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
        self.encoding: dict[str, set[threading.Thread]] = {}
        self.encoding_parts: set[tuple[str, int]] = set()
        self.stitching: dict[str, threading.Thread] = {}
        self.stitch_failures: dict[str, int] = {}
        self.stitch_retry_after: dict[str, float] = {}
        self.analyzing: dict[str, threading.Thread] = {}
        self.uploaded: set[tuple[str, int]] = set()
        self.uploaded_recording_chunks: set[tuple[str, int]] = set()
        self.completed_recording_uploads: set[str] = set()
        self.uploaded_analytics: set[str] = set()
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

    @staticmethod
    def _recording_path(session_dir: Path) -> Path:
        return session_dir / "recording.mp4"

    @staticmethod
    def _preview_path(segment_path: Path) -> Path:
        return segment_path.parent / ".previews" / segment_path.name

    @staticmethod
    def _analytics_path(session_dir: Path) -> Path:
        return session_dir / "analytics.json"

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
        temporary = output.with_name(f"{output.stem}.part{output.suffix}")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            with self.lock:
                self.errors.append("ffmpeg is unavailable; archive segment was retained as MJPEG")
                del self.errors[:-10]
            return
        try:
            # The capture loop's frame rate is not fixed: it depends on the USB
            # camera and the local browser/server load.  Archive parts are cut
            # on wall time, so encode with the observed rate rather than a
            # nominal 30 fps; otherwise a 10-second part recorded at 21 fps
            # plays back as a misleading 7-second clip.
            frame_rate = ARCHIVE_FPS
            if segment.frame_count:
                frame_rate = max(1.0, min(60.0, segment.frame_count / max(0.1, segment.duration_seconds)))
            temporary.unlink(missing_ok=True)
            completed = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "image2pipe", "-framerate", f"{frame_rate:.3f}", "-c:v", "mjpeg", "-i", str(raw_path),
                    "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-b:v", f"{ARCHIVE_VIDEO_MAX_KBPS}k", "-maxrate", f"{ARCHIVE_VIDEO_MAX_KBPS}k",
                    "-bufsize", f"{ARCHIVE_VIDEO_MAX_KBPS * 2}k", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(temporary),
                ],
                capture_output=True,
                timeout=180,
            )
            if completed.returncode != 0 or not temporary.exists() or temporary.stat().st_size == 0:
                detail = completed.stderr.decode(errors="replace").strip()[-500:]
                raise RuntimeError(detail or "ffmpeg produced no MP4")
            temporary.replace(output)
            raw_path.unlink(missing_ok=True)
            if output.stat().st_size > MAX_ARCHIVE_SEGMENT_BYTES:
                self._encode_preview(output, segment.duration_seconds)
        except Exception as exc:  # noqa: BLE001 - live publishing must never stop for archive encoding
            with self.lock:
                self.errors.append(f"archive segment {segment.sequence}: {exc}"[-600:])
                del self.errors[:-10]
        finally:
            temporary.unlink(missing_ok=True)

    def _encode_preview(self, source: Path, duration_seconds: float) -> None:
        """Create a relay-safe version of a long local archive part.

        Full-fidelity parts are retained locally and concatenated into the
        downloadable recording.  This small sibling is only for the public
        in-progress/archive-part view, whose WebSocket message has a hard cap.
        """
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return
        target = self._preview_path(source)
        temporary = target.with_name(f"{target.stem}.part{target.suffix}")
        bitrate_kbps = archive_preview_bitrate_kbps(duration_seconds)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
            completed = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                    "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
                    "-bufsize", f"{bitrate_kbps * 2}k", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(temporary),
                ],
                capture_output=True,
                timeout=300,
            )
            if completed.returncode != 0 or not temporary.exists() or temporary.stat().st_size == 0:
                detail = completed.stderr.decode(errors="replace").strip()[-500:]
                raise RuntimeError(detail or "ffmpeg produced no relay-safe preview")
            if temporary.stat().st_size > MAX_ARCHIVE_SEGMENT_BYTES:
                raise RuntimeError(
                    f"preview is {temporary.stat().st_size:,} bytes; relay limit is {MAX_ARCHIVE_SEGMENT_BYTES:,}"
                )
            temporary.replace(target)
        except Exception as exc:  # noqa: BLE001 - a final recording is still valid without a preview
            with self.lock:
                self.errors.append(f"archive preview {source.name}: {exc}"[-600:])
                del self.errors[:-10]
        finally:
            temporary.unlink(missing_ok=True)

    def _finish_encoding(self, session_id: str, sequence: int, thread: threading.Thread) -> None:
        with self.lock:
            self.encoding_parts.discard((session_id, sequence))
            pending = self.encoding.get(session_id)
            if pending is None:
                return
            pending.discard(thread)
            if not pending:
                self.encoding.pop(session_id, None)

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
            frame_count=frames,
        )

        sidecar = segment.path.with_suffix(".json")
        sidecar.write_text(json.dumps({
            "session_id": segment.session_id,
            "sequence": segment.sequence,
            "started_at": segment.started_at,
            "duration_seconds": segment.duration_seconds,
            "frame_count": segment.frame_count,
        }, separators=(",", ":")))
        self._queue_segment_encoding_locked(raw_path, segment)

    def _queue_segment_encoding_locked(self, raw_path: Path, segment: LocalSegment) -> None:
        key = (segment.session_id, segment.sequence)
        if key in self.encoding_parts:
            return
        if not raw_path.exists() or raw_path.stat().st_size <= 0:
            return
        # A previous worker version encoded directly to the final name.  If it
        # was interrupted it can leave a non-empty but invalid MP4 behind;
        # prefer the retained MJPEG source and rebuild atomically.
        segment.path.unlink(missing_ok=True)

        def encode() -> None:
            try:
                self._encode_segment(raw_path, segment)
            finally:
                self._finish_encoding(segment.session_id, segment.sequence, threading.current_thread())

        thread = threading.Thread(target=encode, daemon=True, name=f"samcam-archive-{segment.sequence}")
        self.encoding_parts.add(key)
        self.encoding.setdefault(segment.session_id, set()).add(thread)
        thread.start()

    def _recover_session_segments_locked(self, metadata: dict[str, Any]) -> None:
        """Resume raw parts left behind by an interrupted or failed encoder.

        Segment details are written *before* ffmpeg starts, so a worker restart
        has enough information to recreate the exact MP4.  Old sessions that
        predate that sidecar convention still get a safe best-effort recovery.
        """
        session_id = str(metadata["session_id"])
        session_dir = self.root / session_id
        for raw_path in sorted(session_dir.glob("segment-*.mjpeg")):
            try:
                sequence = int(raw_path.stem.rsplit("-", 1)[1])
                sidecar = raw_path.with_suffix(".json")
                details = json.loads(sidecar.read_text())
                segment = LocalSegment(
                    session_id=session_id,
                    sequence=sequence,
                    started_at=float(details["started_at"]),
                    duration_seconds=max(0.1, float(details["duration_seconds"])),
                    path=raw_path.with_suffix(".mp4"),
                    frame_count=max(0, int(details.get("frame_count") or 0)),
                )
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    sequence = int(raw_path.stem.rsplit("-", 1)[1])
                    ended_at = raw_path.stat().st_mtime
                except (OSError, ValueError):
                    continue
                started_at = float(metadata.get("started_at") or ended_at)
                segment = LocalSegment(
                    session_id=session_id,
                    sequence=sequence,
                    started_at=started_at,
                    duration_seconds=max(0.1, min(ARCHIVE_SEGMENT_SECONDS, ended_at - started_at)),
                    path=raw_path.with_suffix(".mp4"),
                )
                try:
                    segment.path.with_suffix(".json").write_text(json.dumps({
                        "session_id": segment.session_id,
                        "sequence": segment.sequence,
                        "started_at": segment.started_at,
                        "duration_seconds": segment.duration_seconds,
                        "frame_count": segment.frame_count,
                    }, separators=(",", ":")))
                except OSError:
                    continue
            self._queue_segment_encoding_locked(raw_path, segment)

    def _wait_for_session_encoding(self, session_id: str, timeout: float = 300.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                pending = list(self.encoding.get(session_id, set()))
            if not pending:
                return True
            for thread in pending:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    return False
                thread.join(remaining)

    @staticmethod
    def _ffconcat_path(path: Path) -> str:
        return "'" + str(path.resolve()).replace("'", r"'\''") + "'"

    def _stitch_recording(self, metadata: dict[str, Any]) -> None:
        session_id = str(metadata["session_id"])
        session_dir = self.root / session_id
        output = self._recording_path(session_dir)
        temporary = output.with_name(f"{output.stem}.part{output.suffix}")
        manifest = session_dir / ".recording.ffconcat"
        try:
            if output.exists() and output.stat().st_size > 0:
                return
            with self.lock:
                self._recover_session_segments_locked(metadata)
            if not self._wait_for_session_encoding(session_id):
                raise RuntimeError("timed out waiting for archive parts to encode")
            parts = sorted(session_dir.glob("segment-*.mp4"))
            if not parts:
                raise RuntimeError("no encoded archive parts are available to stitch")
            manifest.write_text("".join(f"file {self._ffconcat_path(path)}\n" for path in parts))
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                raise RuntimeError("ffmpeg is unavailable; could not stitch archive recording")
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(manifest),
                "-c", "copy", "-movflags", "+faststart", str(temporary),
            ]
            completed = subprocess.run(command, capture_output=True, timeout=600)
            if completed.returncode != 0 or not temporary.exists() or temporary.stat().st_size == 0:
                detail = completed.stderr.decode(errors="replace").strip()[-500:]
                raise RuntimeError(detail or "ffmpeg did not create a stitched recording")
            temporary.replace(output)
        except Exception as exc:  # noqa: BLE001 - individual archive recovery must not stop live publishing
            with self.lock:
                self.errors.append(f"archive recording {session_id}: {exc}"[-600:])
                del self.errors[:-10]
                failures = self.stitch_failures.get(session_id, 0) + 1
                self.stitch_failures[session_id] = failures
                # Keep retrying failed/concurrently interrupted sessions, but
                # do not spin ffmpeg every status tick if a disk is full or a
                # local dependency is temporarily unavailable.
                self.stitch_retry_after[session_id] = time.monotonic() + min(30.0, 2.0 ** min(failures, 5))
        finally:
            manifest.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            with self.lock:
                self.stitching.pop(session_id, None)
                if output.exists() and output.stat().st_size > 0:
                    self.stitch_failures.pop(session_id, None)
                    self.stitch_retry_after.pop(session_id, None)
                    self._schedule_analysis_locked(metadata)

    def _schedule_stitch_locked(self, metadata: dict[str, Any]) -> None:
        session_id = str(metadata["session_id"])
        session_dir = self.root / session_id
        output = self._recording_path(session_dir)
        self._recover_session_segments_locked(metadata)
        if session_id in self.stitching or (output.exists() and output.stat().st_size > 0):
            return
        if time.monotonic() < self.stitch_retry_after.get(session_id, 0.0):
            return
        thread = threading.Thread(
            target=self._stitch_recording,
            args=(dict(metadata),),
            daemon=True,
            name=f"samcam-recording-{session_id[-8:]}",
        )
        self.stitching[session_id] = thread
        thread.start()

    def _analyze_recording(self, metadata: dict[str, Any]) -> None:
        session_id = str(metadata["session_id"])
        session_dir = self.root / session_id
        output = self._recording_path(session_dir)
        analytics_path = self._analytics_path(session_dir)
        temporary = analytics_path.with_name(f"{analytics_path.name}.part")
        try:
            if analytics_path.exists() and analytics_path.stat().st_size > 0:
                return
            if not output.exists() or output.stat().st_size <= 0:
                return
            result = analyze_recording(output, metadata)
            temporary.write_text(json.dumps(result, separators=(",", ":")))
            temporary.replace(analytics_path)
        except Exception as exc:  # noqa: BLE001 - analytics never block recording delivery
            with self.lock:
                self.errors.append(f"archive analytics {session_id}: {exc}"[-600:])
                del self.errors[:-10]
        finally:
            temporary.unlink(missing_ok=True)
            with self.lock:
                self.analyzing.pop(session_id, None)

    def _schedule_analysis_locked(self, metadata: dict[str, Any]) -> None:
        session_id = str(metadata["session_id"])
        session_dir = self.root / session_id
        output = self._recording_path(session_dir)
        analytics_path = self._analytics_path(session_dir)
        if (
            session_id in self.analyzing
            or (analytics_path.exists() and analytics_path.stat().st_size > 0)
            or not output.exists()
            or output.stat().st_size <= 0
        ):
            return
        thread = threading.Thread(
            target=self._analyze_recording,
            args=(dict(metadata),),
            daemon=True,
            name=f"samcam-analytics-{session_id[-8:]}",
        )
        self.analyzing[session_id] = thread
        thread.start()

    def schedule_completed_recordings(self) -> None:
        for metadata in self.manifests():
            if metadata.get("ended_at"):
                with self.lock:
                    self._schedule_stitch_locked(metadata)
                    self._schedule_analysis_locked(metadata)

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
            self._schedule_stitch_locked(completed)
            self.active = None
            self.active_dir = None
            return completed

    def reset_uploads(self) -> None:
        with self.lock:
            self.uploaded.clear()
            self.uploaded_recording_chunks.clear()
            self.completed_recording_uploads.clear()
            self.uploaded_analytics.clear()

    def wait_for_encoding(self, timeout: float = 180.0) -> None:
        """Finish MP4 and stitched-recording work before a graceful shutdown."""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                threads = [thread for group in self.encoding.values() for thread in group]
                threads.extend(self.stitching.values())
                threads.extend(self.analyzing.values())
            if not threads:
                return
            for thread in threads:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    return
                thread.join(remaining)

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
        active = self.session()
        active_id = active.get("session_id") if active else None
        # A reconnect can discover many already-backed-up sessions. Always
        # upload the active recording's next part first so Archive updates as
        # the camera runs instead of waiting behind old duplicate checks.
        manifests = sorted(
            self.manifests(),
            key=lambda item: (
                item.get("session_id") != active_id,
                -float(item.get("started_at") or 0),
            ),
        )
        for metadata in manifests:
            session_id = str(metadata["session_id"])
            session_dir = self.root / session_id
            for path in sorted(session_dir.glob("segment-*.mp4")):
                try:
                    sequence = int(path.stem.rsplit("-", 1)[1])
                    if (session_id, sequence) in self.uploaded:
                        continue
                    details = json.loads(path.with_suffix(".json").read_text())
                    preview = self._preview_path(path)
                    upload_path = preview if preview.exists() and preview.stat().st_size > 0 else path
                    return LocalSegment(
                        session_id=session_id,
                        sequence=sequence,
                        started_at=float(details["started_at"]),
                        duration_seconds=float(details["duration_seconds"]),
                        path=upload_path,
                    )
                except (OSError, ValueError, KeyError):
                    continue
        return None

    def mark_uploaded(self, segment: LocalSegment) -> None:
        with self.lock:
            self.uploaded.add((segment.session_id, segment.sequence))

    def next_recording_chunk(self) -> LocalRecordingChunk | None:
        for metadata in sorted(self.manifests(), key=lambda item: -float(item.get("started_at") or 0)):
            if not metadata.get("ended_at"):
                continue
            session_id = str(metadata["session_id"])
            path = self._recording_path(self.root / session_id)
            try:
                size_bytes = path.stat().st_size
            except OSError:
                continue
            if size_bytes <= 0:
                continue
            count = max(1, (size_bytes + ARCHIVE_RECORDING_CHUNK_BYTES - 1) // ARCHIVE_RECORDING_CHUNK_BYTES)
            for index in range(count):
                if (session_id, index) not in self.uploaded_recording_chunks:
                    return LocalRecordingChunk(session_id, path, index, count, size_bytes)
        return None

    def mark_recording_chunk_uploaded(self, chunk: LocalRecordingChunk) -> None:
        with self.lock:
            self.uploaded_recording_chunks.add((chunk.session_id, chunk.index))

    def next_recording_completion(self) -> tuple[str, int, int] | None:
        for metadata in sorted(self.manifests(), key=lambda item: -float(item.get("started_at") or 0)):
            if not metadata.get("ended_at"):
                continue
            session_id = str(metadata["session_id"])
            if session_id in self.completed_recording_uploads:
                continue
            path = self._recording_path(self.root / session_id)
            try:
                size_bytes = path.stat().st_size
            except OSError:
                continue
            if size_bytes <= 0:
                continue
            count = max(1, (size_bytes + ARCHIVE_RECORDING_CHUNK_BYTES - 1) // ARCHIVE_RECORDING_CHUNK_BYTES)
            if all((session_id, index) in self.uploaded_recording_chunks for index in range(count)):
                return session_id, count, size_bytes
        return None

    def mark_recording_complete(self, session_id: str) -> None:
        with self.lock:
            self.completed_recording_uploads.add(session_id)

    def next_analytics(self) -> tuple[str, dict[str, Any]] | None:
        for metadata in sorted(self.manifests(), key=lambda item: -float(item.get("started_at") or 0)):
            if not metadata.get("ended_at"):
                continue
            session_id = str(metadata["session_id"])
            if session_id in self.uploaded_analytics:
                continue
            path = self._analytics_path(self.root / session_id)
            try:
                result = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(result, dict) and isinstance(result.get("samples"), list):
                return session_id, result
        return None

    def mark_analytics_uploaded(self, session_id: str) -> None:
        with self.lock:
            self.uploaded_analytics.add(session_id)


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


async def sync_archive_index(archiver: SessionArchiver, send: Any, exclude_session_id: str | None = None) -> None:
    """Tell a newly connected relay about retained sessions and their text."""
    for metadata in archiver.manifests():
        if metadata.get("session_id") == exclude_session_id:
            continue
        await send({"type": "archive_session", "session": metadata})
        # A reconnect can otherwise leave deleted or corrected historical text
        # in the cloud copy forever. Treat the local archive as canonical.
        await send({
            "type": "archive_transcript_replace",
            "session_id": metadata["session_id"],
            "lines": archiver.transcript_lines(str(metadata["session_id"])),
        })
        analytics_path = archiver._analytics_path(archiver.root / str(metadata["session_id"]))
        try:
            analytics = json.loads(analytics_path.read_text())
        except (OSError, ValueError):
            analytics = None
        if isinstance(analytics, dict):
            await send({
                "type": "archive_analytics",
                "session_id": metadata["session_id"],
                "analytics": analytics,
            })


async def upload_next_segment(archiver: SessionArchiver, send: Any) -> bool:
    segment = archiver.next_segment()
    if segment is None:
        return False
    try:
        data = await asyncio.to_thread(segment.path.read_bytes)
    except OSError:
        return False
    if not data or len(data) > MAX_ARCHIVE_SEGMENT_BYTES:
        print(f"  archive segment {segment.path.name} exceeds the {MAX_ARCHIVE_SEGMENT_BYTES // 1_000_000} MB relay limit", file=sys.stderr)
        archiver.mark_uploaded(segment)
        return False
    metadata = json.dumps({
        "type": "archive_segment",
        "session_id": segment.session_id,
        "sequence": segment.sequence,
        "started_at": segment.started_at,
        "duration_seconds": segment.duration_seconds,
    }, separators=(",", ":")).encode()
    await send(ARCHIVE_MAGIC + len(metadata).to_bytes(4, "big") + metadata + data)
    archiver.mark_uploaded(segment)
    return True


async def upload_next_recording_chunk(archiver: SessionArchiver, send: Any) -> bool:
    chunk = archiver.next_recording_chunk()
    if chunk is None:
        return False
    start = chunk.index * ARCHIVE_RECORDING_CHUNK_BYTES
    try:
        def read_chunk() -> bytes:
            with chunk.path.open("rb") as handle:
                handle.seek(start)
                return handle.read(ARCHIVE_RECORDING_CHUNK_BYTES)
        data = await asyncio.to_thread(read_chunk)
    except OSError:
        return False
    if not data:
        return False
    metadata = json.dumps({
        "type": "archive_recording_chunk",
        "session_id": chunk.session_id,
        "index": chunk.index,
        "count": chunk.count,
        "size_bytes": chunk.size_bytes,
    }, separators=(",", ":")).encode()
    await send(ARCHIVE_RECORDING_MAGIC + len(metadata).to_bytes(4, "big") + metadata + data)
    archiver.mark_recording_chunk_uploaded(chunk)
    return True


async def publish_completed_recording(archiver: SessionArchiver, send: Any) -> None:
    completed = archiver.next_recording_completion()
    if completed is None:
        return
    session_id, chunk_count, size_bytes = completed
    await send({
        "type": "archive_recording_complete",
        "session_id": session_id,
        "chunk_count": chunk_count,
        "size_bytes": size_bytes,
    })
    archiver.mark_recording_complete(session_id)


async def publish_next_analytics(archiver: SessionArchiver, send: Any) -> None:
    pending = archiver.next_analytics()
    if pending is None:
        return
    session_id, analytics = pending
    await send({"type": "archive_analytics", "session_id": session_id, "analytics": analytics})
    archiver.mark_analytics_uploaded(session_id)


async def publish_status(session: aiohttp.ClientSession, send: Any, stop: asyncio.Event, archiver: SessionArchiver) -> None:
    transcript_id = -1
    resumed_session = archiver.session()
    announced_session_id: str | None = None
    needs_transcript_watermark = False
    if resumed_session is not None:
        # Restore this recording as the live session first. Its saved lines are
        # replayed as live events, while all completed sessions stay archive-only.
        await send({"type": "session_start", "session": resumed_session})
        announced_session_id = str(resumed_session["session_id"])
        for line in archiver.transcript_lines(announced_session_id):
            await send({"type": "transcript", "line": line})
        needs_transcript_watermark = True
    archiver.schedule_completed_recordings()
    await sync_archive_index(archiver, send, exclude_session_id=announced_session_id)
    while not stop.is_set():
        # This is intentionally cheap when everything is healthy.  It makes a
        # finished session self-healing after a failed ffmpeg process, a laptop
        # sleep, or a publisher restart instead of leaving it "stitching"
        # until the next manual restart.
        archiver.schedule_completed_recordings()
        stream = await get_json(session, "/api/stream")
        fresh_at = stream.get("last_live_frame_at") if stream else None
        try:
            live = bool(stream and stream.get("live") and fresh_at and time.time() - float(fresh_at) < 5)
        except (TypeError, ValueError):
            live = False
        source = stream.get("source") if stream else None
        transcript = await get_json(session, "/api/transcript")
        active = archiver.session()
        if live and active is not None and needs_transcript_watermark:
            transcript_id = transcript_high_watermark(transcript)
            needs_transcript_watermark = False
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
                    # The transcriber reports time from its own process start.
                    # Archive and public viewers need time from this recording,
                    # otherwise a brand-new live session can look historical.
                    event["started"] = round(
                        max(0.0, event["received_at"] - float(current_session["started_at"])), 2
                    )
                    archiver.append_transcript(event)
                    await send({"type": "transcript", "line": event})
        # Completed recordings must not sit behind a long history of preview
        # parts.  Upload a small bounded batch every tick, then still send one
        # part so Archive can show recoverable previews while the full MP4 is
        # being assembled/uploaded.
        for _ in range(ARCHIVE_RECORDING_CHUNKS_PER_TICK):
            if not await upload_next_recording_chunk(archiver, send):
                break
        await publish_completed_recording(archiver, send)
        await publish_next_analytics(archiver, send)
        await upload_next_segment(archiver, send)
        try:
            await asyncio.wait_for(stop.wait(), timeout=STATUS_POLL_SECONDS)
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


async def publish_audio(session: aiohttp.ClientSession, send: Any, stop: asyncio.Event) -> None:
    """Relay only USB-camera PCM; browser playback happens on the public site.

    ``/stream.pcm`` is a local, endless byte stream. Reframe it into short,
    sample-aligned WebSocket messages so temporary network jitter cannot turn
    into a growing multi-second audio backlog.
    """
    while not stop.is_set():
        try:
            async with session.get(
                f"{LOCAL_URL}/stream.pcm",
                timeout=aiohttp.ClientTimeout(total=None, sock_read=15),
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"local audio returned HTTP {response.status}")
                buffer = bytearray()
                async for chunk in response.content.iter_any():
                    if stop.is_set():
                        return
                    buffer.extend(chunk)
                    while len(buffer) >= LIVE_AUDIO_FRAME_BYTES:
                        pcm = bytes(buffer[:LIVE_AUDIO_FRAME_BYTES])
                        del buffer[:LIVE_AUDIO_FRAME_BYTES]
                        await send(LIVE_AUDIO_MAGIC + pcm)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            print(f"  local audio: {exc}", file=sys.stderr)
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
                    audio_task = asyncio.create_task(publish_audio(session, send, stop))
                    stop_task = asyncio.create_task(stop.wait())
                    done, pending = await asyncio.wait(
                        {status_task, frames_task, audio_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
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
    archiver.wait_for_encoding()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
