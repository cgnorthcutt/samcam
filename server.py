#!/usr/bin/env python3
"""Body-cam live streamer and clip browser.

This Generalplus camera has two USB personalities. It first enumerates as
1b3f:8301 mass storage; a short ON/OFF press while connected is documented to
re-enumerate it as GENERAL-UVC. The UVC personality is captured live through
AVFoundation. Mass-storage mode remains useful for browsing and transcoding the
camera's MJPEG-in-AVI recordings.

Stdlib only. ffmpeg/ffprobe must be on PATH.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from transcript_quality import (
    duplicate_transcript_reason,
    normalize_transcript_text,
    transcript_rejection_reason,
)

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
CACHE = HERE / ".cache"
# Drop any video in here and it joins the library alongside the camera's own
# clips -- useful for demoing the stream without the camera attached.
SOURCES = HERE / "sources"

SCAN_INTERVAL = 1.0  # how fast new footage gets picked up; this is most of the lag
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
VIDEO_EXTS = {".avi", ".mov", ".mp4"}

# Transcode settings. VideoToolbox keeps a 30s 1080p clip at a few seconds;
# the real bottleneck is reading off the camera's USB 2.0 bus.
TARGET_WIDTH = 1280
VIDEO_BITRATE = "4M"


def which_or_die(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        sys.exit(f"error: {binary} not found on PATH (try: brew install ffmpeg)")
    return path


FFMPEG = which_or_die("ffmpeg")
FFPROBE = which_or_die("ffprobe")
TRANSCRIBE_PYTHON = HERE / ".venv" / "bin" / "python"
TRANSCRIBE_WORKER = HERE / "transcribe_worker.py"
NATIVE_CAPTURE = HERE / "bodycam_capture"
TRANSCRIBE_MODEL = os.environ.get(
    "BODYCAM_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"
)


def reap_orphaned_native_helpers() -> list[int]:
    """Stop stale ``bodycam_capture`` children left behind by a crashed server.

    AVCapture holds the UVC video interface open per process.  If a Python
    server is terminated before it runs its capture cleanup, its helper is
    re-parented to launchd (PPID 1) and can leave the next helper with audio
    but no video frames.  Only reap an *exact* helper binary owned by this
    project and only when it is already orphaned; never touch another camera
    client or an intentionally running server child.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    target = NATIVE_CAPTURE.resolve()
    orphans: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        pid_text, parent_text, command = fields
        if parent_text != "1":
            continue
        try:
            pid = int(pid_text)
            argv = shlex.split(command)
            executable = Path(argv[0]).resolve() if argv else None
        except (ValueError, OSError, IndexError):
            continue
        if pid != os.getpid() and executable == target:
            orphans.append(pid)

    for pid in orphans:
        try:
            print(f"  reaping orphaned native camera helper pid={pid}", file=sys.stderr)
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            print(f"  cannot reap native helper pid={pid}", file=sys.stderr)

    # Give AVCapture a brief chance to release its device before opening a new
    # session.  Escalate only the exact stale helper if it ignores SIGTERM.
    deadline = time.monotonic() + 1.5
    remaining = set(orphans)
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
            except PermissionError:
                remaining.discard(pid)
        if remaining:
            time.sleep(0.05)
    for pid in remaining:
        try:
            print(f"  force-stopping stuck native camera helper pid={pid}", file=sys.stderr)
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return orphans


# --------------------------------------------------------------------------
# Camera discovery
# --------------------------------------------------------------------------

@dataclass
class Camera:
    """A mounted camera volume."""
    volume: Path
    video_dir: Path

    @property
    def time_file(self) -> Path:
        return self.volume / "time.txt"


def find_camera() -> Camera | None:
    """Look for a mounted volume that holds a VIDEO/ directory of clips.

    Deliberately not hardcoded to a volume name -- these cameras ship
    unlabeled, so the mount point is usually /Volumes/NO NAME but collides
    with anything else unlabeled and gets renamed to "NO NAME 1".
    """
    storage_attached = any(
        device["vid"] == GENERALPLUS_VID
        and device["pid"] in GENERALPLUS_STORAGE_PIDS
        and USB_CLASS_MASS_STORAGE in device["classes"]
        for device in usb_devices()
    )
    if not storage_attached:
        return None

    for volume in sorted(Path("/Volumes").glob("*")):
        if not volume.is_dir():
            continue
        for name in ("VIDEO", "video", "DCIM"):
            video_dir = volume / name
            try:
                if video_dir.is_dir() and any(
                    p.suffix.lower() in VIDEO_EXTS for p in video_dir.iterdir()
                ):
                    return Camera(volume=volume, video_dir=video_dir)
            except (PermissionError, OSError):
                continue
    return None


# USB class codes we care about.
USB_CLASS_VIDEO = 14      # UVC -- a real camera macOS can open live
USB_CLASS_MASS_STORAGE = 8
GENERALPLUS_VID = 0x1B3F
GENERALPLUS_STORAGE_PIDS = {0x8301}
GENERALPLUS_UVC_PIDS = {
    0x2002,  # 808/Generalplus webcam personality
    0x2202,  # GENERAL-UVC on another Generalplus firmware branch
    0x2247,  # GENERAL WEBCAM / GENERAL-UVC on related Generalplus firmware
}

_usb_cache: tuple[float, list[dict]] = (0.0, [])
_usb_cache_lock = threading.Lock()
USB_CACHE_TTL = 0.5


def usb_devices() -> list[dict]:
    """Enumerate USB devices and the interface classes they expose.

    This is how we tell which mode the camera came up in. These Generalplus
    units are dual-personality: plugged in while off they enumerate as mass
    storage (class 8), and some firmware revisions enumerate as a UVC camera
    (class 14) instead when powered on first. Only the latter can be streamed
    live, so the UI needs to know which one we got.

    Reads ioreg rather than asking AVFoundation, which is faster and does not
    trip the camera permission prompt.
    """
    global _usb_cache
    with _usb_cache_lock:
        cached_at, cached = _usb_cache
        if time.time() - cached_at < USB_CACHE_TTL:
            return cached

    devices: dict[tuple[int, int], dict] = {}
    try:
        proc = subprocess.run(
            ["ioreg", "-w0", "-r", "-c", "IOUSBHostInterface", "-l"],
            capture_output=True, timeout=10,
        )
        blob = proc.stdout.decode(errors="replace")
    except (subprocess.SubprocessError, OSError):
        blob = ""

    for match in re.finditer(r'"USB Device Info" = \{([^}]*)\}', blob):
        fields = dict(re.findall(r'"([^"]+)"=("?[^,"]*"?)', match.group(1)))

        def num(key: str) -> int | None:
            raw = fields.get(key, "").strip('"')
            return int(raw) if raw.isdigit() else None

        vid, pid, cls = num("idVendor"), num("idProduct"), num("bInterfaceClass")
        if vid is None or pid is None:
            continue
        entry = devices.setdefault((vid, pid), {
            "name": fields.get("USB Product Name", "").strip('"') or "unknown",
            "vid": vid, "pid": pid, "classes": [],
        })
        if cls is not None and cls not in entry["classes"]:
            entry["classes"].append(cls)

    result = sorted(devices.values(), key=lambda d: d["name"])
    with _usb_cache_lock:
        _usb_cache = (time.time(), result)
    return result


def usb_status() -> dict:
    """Summarize whether anything on the bus can be streamed live."""
    devices = usb_devices()
    uvc = [d for d in devices if USB_CLASS_VIDEO in d["classes"]]
    bodycam_uvc = [
        d for d in uvc
        if d["vid"] == GENERALPLUS_VID
        and d["pid"] in GENERALPLUS_UVC_PIDS
        and USB_CLASS_VIDEO in d["classes"]
    ]
    cam = next(
        (
            d for d in devices
            if d["vid"] == GENERALPLUS_VID
            and (
                d["pid"] in GENERALPLUS_STORAGE_PIDS
                or d["pid"] in GENERALPLUS_UVC_PIDS
            )
        ),
        None,
    )

    payload = {
        "live_capable": bool(bodycam_uvc),
        "uvc_devices": [d["name"] for d in uvc],
    }
    if cam:
        mode = (
            "uvc-video" if USB_CLASS_VIDEO in cam["classes"]
            else "mass-storage" if USB_CLASS_MASS_STORAGE in cam["classes"]
            else "other"
        )
        payload |= {
            "device": cam["name"],
            "id": f"{cam['vid']:04x}:{cam['pid']:04x}",
            "mode": mode,
        }
    return payload


def volume_usage(volume: Path) -> dict:
    try:
        st = os.statvfs(volume)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total": total, "used": total - free, "free": free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def read_camera_clock(camera: Camera) -> dict | None:
    """Parse the camera's time.txt, which is how these units get their clock.

    The clock resets whenever the battery fully drains, and the wrong time gets
    burned into every frame as a timestamp overlay, so it is worth surfacing.
    """
    try:
        text = camera.time_file.read_text(errors="replace")
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip().upper()] = value.strip()
    try:
        stamp = (
            f"{int(fields['YEAR']):04d}-{int(fields['MONTH']):02d}-{int(fields['DAY']):02d} "
            f"{int(fields['HOUR']):02d}:{int(fields['MINUTE']):02d}:{int(fields['SECOND']):02d}"
        )
    except (KeyError, ValueError):
        return None
    return {"set_to": stamp, "date_stamp_on": fields.get("DATE_STAMP", "").upper().startswith("Y")}


def write_camera_clock(camera: Camera) -> str:
    """Rewrite time.txt with the current local time, preserving the rest.

    The camera reads this file on its next boot, applies it, and (on most
    firmware revisions) deletes it. Only the [DATE_TIME] key/value lines are
    touched; the vendor's trailing notes are left alone.
    """
    now = time.localtime()
    replacements = {
        "YEAR": f"{now.tm_year:04d}",
        "MONTH": f"{now.tm_mon:02d}",
        "DAY": f"{now.tm_mday:02d}",
        "HOUR": f"{now.tm_hour:02d}",
        "MINUTE": f"{now.tm_min:02d}",
        "SECOND": f"{now.tm_sec:02d}",
    }
    try:
        original = camera.time_file.read_text(errors="replace").splitlines()
    except OSError:
        original = ["[DATE_TIME]"] + [f"{k}=" for k in replacements] + ["DATE_STAMP=Y "]

    out, seen = [], set()
    for line in original:
        key = line.partition("=")[0].strip().upper()
        if key in replacements:
            out.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            out.append(line)

    missing = [f"{k}={v}" for k, v in replacements.items() if k not in seen]
    if missing:
        insert_at = next(
            (i + 1 for i, l in enumerate(out) if l.strip().upper() == "[DATE_TIME]"), 0
        )
        out[insert_at:insert_at] = missing

    camera.time_file.write_text("\r\n".join(out) + "\r\n")
    return " ".join(
        (
            f"{replacements['YEAR']}-{replacements['MONTH']}-{replacements['DAY']}",
            f"{replacements['HOUR']}:{replacements['MINUTE']}:{replacements['SECOND']}",
        )
    )


# --------------------------------------------------------------------------
# Clip library
# --------------------------------------------------------------------------

@dataclass
class Clip:
    id: str
    source: Path
    size: int
    mtime: float
    duration: float | None = None
    state: str = "pending"  # pending | working | ready | error
    error: str | None = None
    origin: str = "camera"  # camera | local
    codec: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def fingerprint(self) -> str:
        # Cache key survives restarts but invalidates if the clip is overwritten
        # by loop recording, which reuses filenames.
        return f"{self.id}-{int(self.mtime)}-{self.size}"

    @property
    def mp4(self) -> Path:
        return CACHE / f"{self.fingerprint}.mp4"

    @property
    def thumb(self) -> Path:
        return CACHE / f"{self.fingerprint}.jpg"

    @property
    def analytics(self) -> Path:
        return CACHE / f"{self.fingerprint}.analytics-v4.json"

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.source.name,
            "size": self.size,
            "mtime": self.mtime,
            "duration": self.duration,
            "state": "ready" if self.mp4.exists() else self.state,
            "error": self.error,
            "origin": self.origin,
            "codec": self.codec,
        }


class Library:
    """Tracks clips on the camera and keeps transcoded copies warm."""

    def __init__(self) -> None:
        self.clips: dict[str, Clip] = {}
        self.camera: Camera | None = None
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transcode")
        self.last_scan = 0.0
        self.clock_sync_attempted = False

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def scan(self) -> None:
        camera = find_camera()
        sync_clock = False
        with self.lock:
            self.camera = camera
            if camera is None:
                # A later mount is a new connection and should receive a fresh
                # host timestamp.
                self.clock_sync_attempted = False
            elif not self.clock_sync_attempted:
                self.clock_sync_attempted = True
                sync_clock = True

        if sync_clock and camera is not None:
            try:
                stamp = write_camera_clock(camera)
                print(f"  camera clock synced automatically to {stamp}")
            except OSError as exc:
                print(f"  camera clock sync failed: {exc}", file=sys.stderr)

        # The camera when present, plus anything dropped in sources/. Local
        # files keep the library usable with the camera unplugged.
        roots: list[tuple[Path, str]] = []
        if camera is not None:
            roots.append((camera.video_dir, "camera"))
        if SOURCES.is_dir():
            roots.append((SOURCES, "local"))

        found: dict[str, Clip] = {}
        for root, origin in roots:
            try:
                entries = sorted(root.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.suffix.lower() not in VIDEO_EXTS or path.name.startswith("."):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                clip_id = path.stem
                if not SAFE_ID.match(clip_id) or clip_id in found:
                    continue
                found[clip_id] = Clip(
                    id=clip_id, source=path, size=st.st_size,
                    mtime=st.st_mtime, origin=origin,
                )

        with self.lock:
            for clip_id, fresh in found.items():
                existing = self.clips.get(clip_id)
                # Loop recording reuses filenames, so a changed size or mtime
                # means this is a different clip that needs re-encoding.
                if existing and (existing.size, int(existing.mtime)) == (
                    fresh.size,
                    int(fresh.mtime),
                ):
                    continue
                self.clips[clip_id] = fresh
                self.pool.submit(self._prepare, fresh)
            for gone in set(self.clips) - set(found):
                # UVC mode unmounts the camera. Keep its cached archive entries
                # available until storage mode returns and gives us an
                # authoritative directory listing.
                if camera is None and self.clips[gone].origin == "camera":
                    continue
                del self.clips[gone]
            self.last_scan = time.time()

    def _prepare(self, clip: Clip) -> None:
        with clip.lock:
            if clip.mp4.exists() and clip.thumb.exists():
                clip.state = "ready"
                # Still needed on the cached path: the streamer decides between
                # copying and encoding frames based on the codec.
                if clip.duration is None:
                    clip.duration = probe_duration(clip.source)
                if clip.codec is None:
                    clip.codec = probe_codec(clip.source)
                return
            clip.state = "working"
            try:
                CACHE.mkdir(exist_ok=True)
                clip.duration = probe_duration(clip.source)
                clip.codec = probe_codec(clip.source)
                make_thumbnail(clip.source, clip.thumb)
                transcode(clip.source, clip.mp4)
                clip.state = "ready"
                clip.error = None
            except subprocess.CalledProcessError as exc:
                clip.state = "error"
                clip.error = (exc.stderr or b"").decode(errors="replace").strip()[-400:]
            except Exception as exc:  # noqa: BLE001 - surface anything to the UI
                clip.state = "error"
                clip.error = str(exc)

    def ordered(self) -> list[Clip]:
        with self.lock:
            return sorted(self.clips.values(), key=lambda c: (c.mtime, c.id), reverse=True)

    def get(self, clip_id: str) -> Clip | None:
        with self.lock:
            return self.clips.get(clip_id)

    def status(self) -> dict:
        with self.lock:
            camera = self.camera
            clips = list(self.clips.values())
        ready = sum(1 for c in clips if c.mp4.exists())
        working = sum(1 for c in clips if c.state == "working")
        payload = {
            "connected": camera is not None,
            # AVFoundation is authoritative in UVC mode; this firmware's USB
            # interface metadata disappears after macOS claims the device.
            "live_available": find_live_camera() is not None,
            "clip_count": len(clips),
            "ready_count": ready,
            "working_count": working,
            "last_scan": self.last_scan,
            "usb": usb_status(),
        }
        if camera:
            payload |= {
                "volume": str(camera.volume),
                "video_dir": str(camera.video_dir),
                "storage": volume_usage(camera.volume),
                "clock": read_camera_clock(camera),
                "host_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        return payload


# --------------------------------------------------------------------------
# Continuous stream
# --------------------------------------------------------------------------

SOI = b"\xff\xd8\xff"          # JPEG start-of-image
EOI = b"\xff\xd9"              # JPEG end-of-image
DEFAULT_FPS = 25.0
# `bodycam_capture` normalizes the camera microphone to this format before
# handing it to us.  Keeping it raw locally avoids a second lossy encode before
# the public viewer's Web Audio buffer receives it.
LIVE_AUDIO_SAMPLE_RATE = 16_000
LIVE_AUDIO_CHANNELS = 1
LIVE_AUDIO_BYTES_PER_SAMPLE = 2
# Feed the publisher roughly 100 ms at a time. A one-second pipe read would
# make the Web Audio jitter buffer start an entire second behind the camera.
LIVE_AUDIO_PUBLISH_CHUNK_BYTES = 3_200
# The camera has to switch USB personalities before macOS can enumerate its
# UVC interface, so no application can make that physical step instantaneous.
# Once AVFoundation does report the device, however, fail a no-frame capture
# quickly and reopen it instead of making viewers wait through a 10s dead
# session (and then exponential backoff).
LIVE_START_TIMEOUT = 4.0       # device present but never produced its first frame
# A hardware button press can leave the UVC device enumerated briefly even
# after it stops delivering frames. Keep the frozen-frame window short; the
# UI independently reacts to the mode change as soon as macOS reports it.
LIVE_STALL_TIMEOUT = 1.5       # device stopped delivering frames without disconnecting
LIVE_RETRY_INITIAL = 0.25
LIVE_RETRY_MAX = 2.0
LIVE_DISCOVERY_INTERVAL = 0.25
LIVE_CAPTURE_PROFILES = (
    # Confirmed native mode for the 1b3f:2002 GENERAL - UVC firmware.
    ("1280x720@30", (
        "-pixel_format", "uyvy422",
        "-framerate", "30", "-video_size", "1280x720",
    )),
    ("default", ()),
    ("640x480@30", ("-framerate", "30", "-video_size", "640x480")),
    ("1920x1080@30", ("-framerate", "30", "-video_size", "1920x1080")),
    ("640x480@10", ("-framerate", "10", "-video_size", "640x480")),
)


def jpeg_frames(stream):
    """Yield complete JPEGs from ffmpeg's image2pipe output.

    Splitting on EOI publishes a frame as soon as it is complete. The older
    next-SOI splitter always held one full frame back, adding needless latency
    to the live path and dropping the final frame when a device disconnected.
    """
    buffer = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        buffer.extend(chunk)

        while True:
            start = buffer.find(SOI)
            if start < 0:
                # SOI is three bytes and may straddle reads.
                if len(buffer) > 2:
                    del buffer[:-2]
                break
            if start:
                del buffer[:start]

            end = buffer.find(EOI, len(SOI))
            if end < 0:
                break
            end += len(EOI)
            yield bytes(buffer[:end])
            del buffer[:end]


class Streamer:
    """Publish one live bodycam feed to every connected MJPEG viewer."""

    def __init__(self, transcriber: "Transcriber | None" = None) -> None:
        self.transcriber = transcriber
        self.frame: bytes | None = None
        self.seq = 0
        self.source: str | None = None
        self.fps = DEFAULT_FPS
        self.condition = threading.Condition()
        self.viewers = 0
        self.audio_viewers = 0
        self.audio_seq = 0
        # Retain only a short recovery window for a slow local publisher. Live
        # audio must drop old data rather than accumulate seconds of delay.
        self.audio_packets: deque[tuple[int, bytes]] = deque(maxlen=80)
        self.started = False
        self.lock = threading.Lock()
        self.live = False  # true while a real UVC feed is on screen
        self.live_profile: str | None = None
        self.live_error: str | None = None
        self.last_live_frame_at: float | None = None
        self.live_retry_delay = LIVE_RETRY_INITIAL
        self.stopping = threading.Event()
        self._native_process_lock = threading.Lock()
        self._native_process: subprocess.Popen[bytes] | None = None

    # -- lifecycle -------------------------------------------------------
    def ensure_running(self) -> None:
        with self.lock:
            if self.started:
                return
            self.started = True
            threading.Thread(target=self._produce, daemon=True).start()

    def stop(self) -> None:
        self.stopping.set()
        # Do not rely on the producer thread getting another scheduling slice
        # before a server shutdown.  Explicitly signal the native helper so it
        # releases AVFoundation before the Python parent exits.
        with self._native_process_lock:
            native_process = self._native_process
        if native_process is not None:
            self._stop_native_process(native_process)
        self.clear_frame()

    @staticmethod
    def _stop_native_process(proc: subprocess.Popen[bytes]) -> None:
        """End one native helper and, if needed, its dedicated process group."""
        if proc.poll() is not None:
            return
        try:
            # The helper is started in a new session, so this cannot signal
            # the server or a terminal that happens to own the server.
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    def publish(self, jpeg: bytes) -> None:
        with self.condition:
            self.frame = jpeg
            self.seq += 1
            self.condition.notify_all()

    def publish_audio(self, pcm: bytes) -> None:
        """Fan out body-camera 16 kHz mono s16le PCM to local subscribers."""
        if not pcm:
            return
        # A partial Int16 sample cannot be played by Web Audio. It is safe to
        # discard the one trailing byte: every capture packet is sample-aligned
        # and this only protects the fallback FFmpeg pipe.
        if len(pcm) % LIVE_AUDIO_BYTES_PER_SAMPLE:
            pcm = pcm[:-1]
        if not pcm:
            return
        with self.condition:
            self.audio_seq += 1
            self.audio_packets.append((self.audio_seq, pcm))
            self.condition.notify_all()

    def clear_frame(self) -> None:
        """Clear a stale image once when live capture stops."""
        with self.condition:
            if self.frame is None:
                return
            self.frame = None
            self.seq += 1
            self.condition.notify_all()

    def add_viewer(self) -> None:
        with self.condition:
            self.viewers += 1

    def remove_viewer(self) -> None:
        with self.condition:
            self.viewers = max(0, self.viewers - 1)

    def add_audio_viewer(self) -> None:
        with self.condition:
            self.audio_viewers += 1

    def remove_audio_viewer(self) -> None:
        with self.condition:
            self.audio_viewers = max(0, self.audio_viewers - 1)

    def wait_for_frame(self, last_seq: int, timeout: float = 5.0) -> tuple[bytes | None, int]:
        with self.condition:
            if self.seq == last_seq:
                self.condition.wait(timeout)
            return self.frame, self.seq

    def wait_for_audio(
        self, last_seq: int, timeout: float = 1.0
    ) -> tuple[list[bytes], int]:
        """Return packets newer than ``last_seq`` without replaying stale audio."""
        with self.condition:
            if self.audio_seq <= last_seq:
                self.condition.wait(timeout)
            packets = [payload for sequence, payload in self.audio_packets if sequence > last_seq]
            return packets, self.audio_seq

    def status(self) -> dict:
        return {
            "source": self.source,
            "fps": round(self.fps, 1),
            "frames": self.seq,
            "viewers": self.viewers,
            "audio_viewers": self.audio_viewers,
            "audio_packets": self.audio_seq,
            "running": self.started,
            "live": self.live,
            "live_profile": self.live_profile,
            "live_error": self.live_error,
            "last_live_frame_at": self.last_live_frame_at,
        }

    # -- producer --------------------------------------------------------
    def _capture_needed(self) -> bool:
        with self.condition:
            viewers_need_capture = self.viewers > 0 or self.audio_viewers > 0
        return not self.stopping.is_set() and (
            viewers_need_capture
            or (self.transcriber is not None and self.transcriber.is_running())
        )

    def _audio_needed(self) -> bool:
        """Whether the one camera capture session must expose audio."""
        with self.condition:
            public_audio_needed = self.audio_viewers > 0
        return public_audio_needed or (
            self.transcriber is not None and self.transcriber.is_running()
        )

    def _produce(self) -> None:
        while not self.stopping.is_set():
            # Nobody watching means no reason to spin ffmpeg and hammer the
            # camera's USB 2.0 bus; idle until a viewer shows up.
            if not self._capture_needed():
                self.source = None
                time.sleep(0.5)
                continue

            # A live UVC feed beats anything on disk. This is the real thing:
            # the camera in webcam mode, streaming as it sees.
            live = find_live_camera()
            if live is not None:
                try:
                    self._play_live(*live)
                except Exception as exc:  # noqa: BLE001 - never kill the stream
                    self.live_error = str(exc)
                    print(f"  live capture error: {exc}", file=sys.stderr)
                    time.sleep(self.live_retry_delay)
                    self.live_retry_delay = min(
                        self.live_retry_delay * 2.0, LIVE_RETRY_MAX
                    )
                continue

            # The Stream tab is live-only. Archived recordings belong in the
            # Archive tab and must never begin playing just because USB live
            # capture disappeared.
            self.source = None
            self.live = False
            self.live_profile = None
            self.clear_frame()
            # Polling is deliberately faster than the USB personality change
            # so the first live frame follows camera enumeration promptly.
            time.sleep(LIVE_DISCOVERY_INTERVAL)

    def _play_live(self, selector: str, name: str) -> None:
        """Stream the camera's live UVC feed straight through.

        Frames come off AVFoundation and go out as JPEG with no file involved,
        so this is genuinely live -- the recorded-clip path exists only because
        the camera refuses to be a camera in mass-storage mode.
        """
        self.live = True
        self.source = f"LIVE · {name}"
        self.fps = 30.0
        failures = []
        try:
            if NATIVE_CAPTURE.exists() and os.access(NATIVE_CAPTURE, os.X_OK):
                try:
                    self.live_profile = "native AVFoundation · 1280x720@30"
                    self._capture_native()
                    return
                except RuntimeError as exc:
                    # The native path is specifically here to avoid repeatedly
                    # opening this fragile camera through FFmpeg after a stall.
                    # Release it fully, back off, and let the next reconnect use
                    # a fresh AVCaptureSession.
                    raise RuntimeError(f"native AVFoundation: {exc}") from exc

            for label, input_options in LIVE_CAPTURE_PROFILES:
                if not self._capture_needed() or find_live_camera() is None:
                    return
                try:
                    self.live_profile = label
                    self._capture_live_profile(selector, input_options)
                    return
                except RuntimeError as exc:
                    failures.append(f"{label}: {exc}")
                    # Generalplus firmware variants expose different defaults.
                    # Move through a small known-good matrix instead of retrying
                    # one unsupported mode forever.
                    time.sleep(0.25)
            raise RuntimeError("all live capture profiles failed:\n" + "\n".join(failures))
        finally:
            self.live = False
            self.live_profile = None
            self.last_live_frame_at = None
            if self.source == f"LIVE · {name}":
                self.source = None
            # Never leave the last camera JPEG on screen while AVFoundation
            # tears down after the camera button switches USB modes.
            self.clear_frame()

    def _capture_native(self) -> None:
        """Read interleaved JPEG/PCM packets from one native AVCaptureSession."""
        # A previous server crash can leave one helper parented by launchd.
        # Reap it before opening AVCapture, otherwise UVC may provide audio
        # while withholding every video frame from this new session.
        reap_orphaned_native_helpers()
        proc = subprocess.Popen(
            [str(NATIVE_CAPTURE)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            # The helper has no children today, but an isolated group gives us
            # a reliable teardown boundary if that ever changes.
            start_new_session=True,
            env=os.environ | {"BODYCAM_PARENT_PID": str(os.getpid())},
        )
        with self._native_process_lock:
            self._native_process = proc
        errors: deque[str] = deque(maxlen=30)
        frames: queue.Queue[bytes | None] = queue.Queue(maxsize=3)
        audio_bytes = 0

        def read_exact(stream, size: int) -> bytes:
            data = bytearray()
            while len(data) < size:
                chunk = stream.read(size - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            return bytes(data)

        def drain_errors() -> None:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode(errors="replace").strip()
                if line:
                    errors.append(line)

        def read_packets() -> None:
            nonlocal audio_bytes
            try:
                while True:
                    header = read_exact(proc.stdout, 5)
                    if len(header) != 5:
                        return
                    kind = header[0]
                    length = int.from_bytes(header[1:], "big")
                    if length <= 0 or length > 20_000_000:
                        errors.append(f"invalid native packet length {length}")
                        return
                    payload = read_exact(proc.stdout, length)
                    if len(payload) != length:
                        return
                    if kind == ord("V"):
                        try:
                            frames.put_nowait(payload)
                        except queue.Full:
                            try:
                                frames.get_nowait()
                            except queue.Empty:
                                pass
                            frames.put_nowait(payload)
                    elif kind == ord("A"):
                        audio_bytes += len(payload)
                        self.publish_audio(payload)
                        if (
                            self.transcriber is not None
                            and self.transcriber.is_running()
                        ):
                            self.transcriber.feed_audio(payload)
            finally:
                try:
                    frames.put_nowait(None)
                except queue.Full:
                    try:
                        frames.get_nowait()
                    except queue.Empty:
                        pass
                    frames.put_nowait(None)

        threading.Thread(target=drain_errors, daemon=True).start()
        threading.Thread(target=read_packets, daemon=True).start()

        published = 0
        last_frame_at = time.monotonic()
        previous_frame_at: float | None = None
        try:
            while True:
                if not self._capture_needed() or find_live_camera() is None:
                    return
                try:
                    frame = frames.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    timeout = LIVE_STALL_TIMEOUT if published else LIVE_START_TIMEOUT
                    if time.monotonic() - last_frame_at > timeout:
                        raise RuntimeError(
                            f"native capture stalled for {timeout:.0f}s "
                            f"(audio bytes={audio_bytes})"
                        )
                    continue

                if frame is None:
                    break
                self.publish(frame)
                published += 1
                now = time.monotonic()
                self.live_error = None
                self.last_live_frame_at = time.time()
                self.live_retry_delay = LIVE_RETRY_INITIAL
                if previous_frame_at is not None:
                    instantaneous = 1.0 / max(now - previous_frame_at, 0.001)
                    if 1.0 <= instantaneous <= 120.0:
                        self.fps = self.fps * 0.9 + instantaneous * 0.1
                previous_frame_at = now
                last_frame_at = now

            if self._capture_needed() and find_live_camera() is not None:
                detail = "\n".join(list(errors)[-8:])
                if not detail:
                    detail = f"native helper exited with status {proc.poll()}"
                raise RuntimeError(detail)
        finally:
            self._stop_native_process(proc)
            with self._native_process_lock:
                if self._native_process is proc:
                    self._native_process = None
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

    def _capture_live_profile(
        self, selector: str, input_options: tuple[str, ...]
    ) -> None:
        """Run one AVFoundation format until disconnect, stall, or viewer exit."""
        audio_enabled = self._audio_needed()
        audio_read_fd: int | None = None
        audio_write_fd: int | None = None
        audio_args: list[str] = []
        input_target = f"{selector}:none"
        if audio_enabled:
            audio_read_fd, audio_write_fd = os.pipe()
            audio_device = (
                self.transcriber.device
                if self.transcriber is not None
                else "GENERAL - AUDIO"
            )
            input_target = f"{selector}:{audio_device}"
            audio_args = [
                "-map", "0:a:0", "-vn",
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                "-f", "s16le", f"pipe:{audio_write_fd}",
            ]

        try:
            proc = subprocess.Popen(
                [
                    FFMPEG, "-hide_banner", "-loglevel", "warning",
                    "-fflags", "nobuffer",
                    "-f", "avfoundation", *input_options, "-i", input_target,
                    "-map", "0:v:0", "-an",
                    "-c:v", "mjpeg", "-q:v", "5",
                    "-flush_packets", "1", "-f", "image2pipe", "pipe:1",
                    *audio_args,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
                pass_fds=(audio_write_fd,) if audio_write_fd is not None else (),
            )
        except Exception:
            for fd in (audio_read_fd, audio_write_fd):
                if fd is not None:
                    os.close(fd)
            raise

        if audio_write_fd is not None:
            os.close(audio_write_fd)

        def pump_audio() -> None:
            if audio_read_fd is None:
                return
            try:
                with os.fdopen(audio_read_fd, "rb", buffering=0) as audio:
                    for chunk in iter(
                        lambda: audio.read(LIVE_AUDIO_PUBLISH_CHUNK_BYTES), b""
                    ):
                        self.publish_audio(chunk)
                        if (
                            self.transcriber is not None
                            and self.transcriber.is_running()
                        ):
                            self.transcriber.feed_audio(chunk)
            except OSError:
                pass

        if audio_enabled:
            threading.Thread(target=pump_audio, daemon=True).start()

        errors: list[str] = []
        frames: queue.Queue[bytes | None] = queue.Queue(maxsize=3)

        def drain_errors() -> None:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode(errors="replace").strip()
                if line:
                    errors.append(line)
                    del errors[:-20]

        def read_frames() -> None:
            try:
                for frame in jpeg_frames(proc.stdout):
                    try:
                        frames.put_nowait(frame)
                    except queue.Full:
                        # Keep latency bounded. If publishing ever falls behind,
                        # discard the oldest frame rather than building a delay.
                        try:
                            frames.get_nowait()
                        except queue.Empty:
                            pass
                        frames.put_nowait(frame)
            finally:
                try:
                    frames.put_nowait(None)
                except queue.Full:
                    try:
                        frames.get_nowait()
                    except queue.Empty:
                        pass
                    frames.put_nowait(None)

        threading.Thread(target=drain_errors, daemon=True).start()
        threading.Thread(target=read_frames, daemon=True).start()
        published = 0
        last_frame_at = time.monotonic()
        previous_frame_at: float | None = None
        try:
            while True:
                if not self._capture_needed() or find_live_camera() is None:
                    return
                # Starting or stopping transcription changes whether the one
                # camera-owning process needs an audio output. Restart cleanly
                # so a second AVFoundation process is never opened.
                if self._audio_needed() != audio_enabled:
                    return

                try:
                    frame = frames.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    timeout = LIVE_STALL_TIMEOUT if published else LIVE_START_TIMEOUT
                    if time.monotonic() - last_frame_at > timeout:
                        raise RuntimeError(
                            f"live capture stalled for {timeout:.0f}s"
                        )
                    continue

                if frame is None:
                    break
                self.publish(frame)
                published += 1
                now = time.monotonic()
                self.live_error = None
                self.last_live_frame_at = time.time()
                self.live_retry_delay = LIVE_RETRY_INITIAL
                if previous_frame_at is not None:
                    instantaneous = 1.0 / max(now - previous_frame_at, 0.001)
                    if 1.0 <= instantaneous <= 120.0:
                        self.fps = self.fps * 0.9 + instantaneous * 0.1
                previous_frame_at = now
                last_frame_at = now

            code = proc.poll()
            if self._capture_needed() and find_live_camera() is not None:
                detail = "\n".join(errors[-5:]) or f"ffmpeg exited with status {code}"
                if not published:
                    detail = f"no live frames received; {detail}"
                raise RuntimeError(detail)
        finally:
            proc.kill()
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

_live_cache: tuple[float, tuple[str, str] | None] = (0.0, None)
_live_lock = threading.Lock()
# Device discovery invokes AVFoundation through ffmpeg, so retain a small
# cache. Half a second keeps the process light without making a newly
# enumerated UVC camera feel sluggish.
LIVE_DEVICE_CACHE_TTL = 0.5


def list_capture_devices() -> list[tuple[int, str]]:
    """Video capture devices AVFoundation can see, as (index, name)."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, timeout=20,
    )
    devices, in_video = [], False
    for line in proc.stderr.decode(errors="replace").splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            in_video = False
            continue
        match = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if in_video and match:
            devices.append((int(match.group(1)), match.group(2)))
    return devices


def find_live_camera() -> tuple[str, str] | None:
    """The body cam as a live capture device, if it is in UVC mode.

    Only meaningful once the camera has re-enumerated as 1b3f:2002 -- in its
    default 8301 mass-storage personality it is not a camera at all. Built-in
    Mac cameras are excluded: streaming those would defeat the point.
    """
    global _live_cache
    with _live_lock:
        cached_at, cached = _live_cache
        if time.time() - cached_at < LIVE_DEVICE_CACHE_TTL:
            return cached

    result = None
    try:
        # On this exact 1b3f:2002 firmware, macOS's UVC driver claims the
        # interfaces and the IOUSBHostInterface nodes no longer carry the
        # "USB Device Info" property used by usb_devices(). AVFoundation still
        # exposes the unambiguous vendor name "GENERAL - UVC", so use that as
        # the authoritative live-ready signal. It cannot match the Mac/iPhone
        # cameras or screen capture.
        candidate = next(
            (
                (index, name) for index, name in list_capture_devices()
                if "general" in name.lower() and "uvc" in name.lower()
            ),
            None,
        )
        if candidate is not None:
            _, name = candidate
            # AVFoundation accepts a device name as the input selector. Unlike
            # a numeric index, it remains stable across reconnects.
            result = (name, name)
    except (subprocess.SubprocessError, OSError):
        result = None

    with _live_lock:
        _live_cache = (time.time(), result)
    return result


def probe_codec(source: Path) -> str | None:
    """Video codec name, which decides whether frames can be copied or must be encoded."""
    try:
        proc = run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", str(source)],
            timeout=60,
        )
        return proc.stdout.decode().strip() or None
    except subprocess.SubprocessError:
        return None


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, check=True, timeout=timeout)


def probe_duration(source: Path) -> float | None:
    try:
        proc = run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(source)],
            timeout=60,
        )
        return round(float(proc.stdout.decode().strip()), 2)
    except (subprocess.SubprocessError, ValueError):
        return None


def make_thumbnail(source: Path, dest: Path) -> None:
    # Written to a .part file first so a half-finished image is never served;
    # that hides the extension from ffmpeg, hence the explicit -f.
    tmp = dest.with_name(dest.name + ".part")
    # Seek a second in; frame zero on these cameras is often a dark half-frame.
    run([FFMPEG, "-y", "-v", "error", "-ss", "1", "-i", str(source),
         "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5",
         "-f", "image2", str(tmp)], timeout=120)
    tmp.replace(dest)


def transcode(source: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".part")
    run([
        FFMPEG, "-y", "-v", "error",
        "-i", str(source),
        "-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE,
        "-vf", f"scale={TARGET_WIDTH}:-2",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        "-f", "mp4", str(tmp),
    ])
    tmp.replace(dest)


def analyze_clip(clip: Clip) -> dict:
    """Sample a clip into honest video-derived and estimated product metrics."""
    with clip.lock:
        if clip.analytics.exists():
            try:
                return json.loads(clip.analytics.read_text())
            except (OSError, ValueError):
                pass

        source = clip.mp4
        if not source.exists():
            raise FileNotFoundError("browser-ready video is unavailable for analysis")

        duration = clip.duration or probe_duration(source) or 0.0
        if duration <= 0:
            raise RuntimeError("video duration is unavailable")

        # Cap work at roughly 180 tiny grayscale frames, regardless of clip
        # length. This is fast enough for an on-demand dashboard while still
        # preserving the shape of motion and lighting across the whole video.
        sample_fps = min(1.0, max(0.05, 180.0 / duration))
        width, height = 64, 36
        frame_bytes = width * height
        proc = run(
            [
                FFMPEG, "-v", "error", "-i", str(source),
                "-vf",
                f"fps={sample_fps:.6f},scale={width}:{height}:flags=area:out_range=full,format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
            ],
            timeout=180,
        )
        raw = proc.stdout
        frame_count = len(raw) // frame_bytes
        if frame_count == 0:
            raise RuntimeError("ffmpeg returned no analyzable frames")

        samples: list[dict] = []
        previous: bytes | None = None
        for index in range(frame_count):
            frame = raw[index * frame_bytes:(index + 1) * frame_bytes]
            luminance = sum(frame) / (frame_bytes * 255.0) * 100.0
            if previous is None:
                motion = 0.0
            else:
                mean_delta = sum(
                    abs(current - prior)
                    for current, prior in zip(frame, previous)
                ) / frame_bytes
                # Two-times scaling turns a raw full-frame pixel delta into a
                # useful 0-100 motion index; it is a relative index, not an IMU.
                sample_interval = 1.0 / sample_fps
                motion = min(
                    100.0, mean_delta / 255.0 * 200.0 / sample_interval
                )
            previous = frame

            lighting = max(0.0, 100.0 - abs(luminance - 50.0) * 2.2)
            stability = max(0.0, 100.0 - motion)
            capture_quality = lighting * 0.55 + stability * 0.45
            samples.append({
                "time": round(min(duration, (index + 0.5) / sample_fps), 2),
                "motion": round(motion, 1),
                "luminance": round(luminance, 1),
                "lighting": round(lighting, 1),
                "stability": round(stability, 1),
                "quality": round(capture_quality, 1),
            })

        # The first frame has no predecessor, so exclude its zero placeholder
        # from aggregate motion and stability statistics.
        motion_samples = samples[1:] if len(samples) > 1 else samples
        motions = sorted(sample["motion"] for sample in motion_samples)
        p95_index = min(len(motions) - 1, int(len(motions) * 0.95))
        result = {
            "clip": clip.to_json(),
            "generated_at": time.time(),
            "sample_fps": round(sample_fps, 4),
            "samples": samples,
            "summary": {
                "average_motion": round(
                    sum(sample["motion"] for sample in motion_samples)
                    / len(motion_samples),
                    1,
                ),
                "peak_motion_p95": motions[p95_index],
                "average_lighting": round(
                    sum(sample["lighting"] for sample in samples) / len(samples), 1
                ),
                "average_quality": round(
                    sum(sample["quality"] for sample in samples) / len(samples), 1
                ),
                "stable_share": round(
                    100.0
                    * sum(sample["motion"] < 25 for sample in motion_samples)
                    / len(motion_samples),
                    1,
                ),
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
                "video_derived": [
                    "video duration",
                    "sampled frame luminance",
                    "sampled frame-to-frame motion",
                ],
                "model_assumptions": [
                    "lighting quality favors mid-range luminance",
                    "capture index weights lighting 55% and stability 45%",
                ],
                "estimated": [
                    "battery remaining and ETA",
                    "effective worn load and neck torque",
                    "ergonomic and market-fit scores",
                ],
                "limitations": [
                    "battery values are listing-based estimates, not telemetry",
                    "motion is a frame-difference index, not physical acceleration",
                    "moving subjects and camera movement are not separated",
                    "lighting quality is a grayscale exposure heuristic",
                    "ergonomic and suitability scores are unvalidated scenario models",
                ],
            },
            "schema_version": 4,
        }
        tmp = clip.analytics.with_name(clip.analytics.name + ".part")
        tmp.write_text(json.dumps(result, separators=(",", ":")))
        tmp.replace(clip.analytics)
        return result


# --------------------------------------------------------------------------
# Live local transcription
# --------------------------------------------------------------------------

class Transcriber:
    """Supervise MLX Whisper and accept PCM from the combined camera capture."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
        self.audio_stop: threading.Event | None = None
        self.state = "stopped"
        self.error: str | None = None
        self.model = TRANSCRIBE_MODEL
        self.device = "GENERAL - AUDIO"
        self.seq = 0
        self.started_at: float | None = None
        self.last_event_at: float | None = None
        self.audio_received_bytes = 0
        self.audio_written_bytes = 0
        self.audio_dropped_bytes = 0
        self.chunk_seconds = 3.0
        self.silence_events = 0
        self.last_rms_db: float | None = None
        self.lines: deque[dict] = deque(maxlen=200)
        # A second gate lives in the parent process so a buggy/restarted MLX
        # worker can never make invented text visible or archive it.
        self.recent_transcript_texts: deque[str] = deque(maxlen=12)
        self.diagnostics: deque[str] = deque(maxlen=30)

    def start(self) -> dict:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return self.status_locked()
            if not TRANSCRIBE_PYTHON.exists():
                self.state = "unavailable"
                self.error = (
                    "transcription environment missing; run "
                    "uv venv --python 3.12 .venv && "
                    "uv pip install --python .venv/bin/python "
                    "-r requirements-transcription.txt"
                )
                return self.status_locked()
            if not TRANSCRIBE_WORKER.exists():
                self.state = "unavailable"
                self.error = "transcribe_worker.py is missing"
                return self.status_locked()

            self.state = "starting"
            self.error = None
            self.started_at = time.time()
            self.audio_received_bytes = 0
            self.audio_written_bytes = 0
            self.audio_dropped_bytes = 0
            self.silence_events = 0
            self.last_rms_db = None
            self.recent_transcript_texts.clear()
            while True:
                try:
                    self.audio_queue.get_nowait()
                except queue.Empty:
                    break
            self.process = subprocess.Popen(
                [str(TRANSCRIBE_PYTHON), str(TRANSCRIBE_WORKER)],
                cwd=HERE,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=os.environ | {"BODYCAM_WHISPER_MODEL": self.model},
            )
            process = self.process
            audio_stop = threading.Event()
            self.audio_stop = audio_stop

        threading.Thread(
            target=self._write_audio, args=(process, audio_stop), daemon=True
        ).start()
        threading.Thread(
            target=self._read_events, args=(process,), daemon=True
        ).start()
        threading.Thread(
            target=self._read_diagnostics, args=(process,), daemon=True
        ).start()
        return self.status()

    def stop(self) -> dict:
        with self.lock:
            process = self.process
            self.process = None
            audio_stop = self.audio_stop
            self.audio_stop = None
            self.state = "stopped"
        if audio_stop is not None:
            audio_stop.set()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        return self.status()

    def is_running(self) -> bool:
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def feed_audio(self, data: bytes) -> None:
        if not data or not self.is_running():
            return
        with self.lock:
            self.audio_received_bytes += len(data)
        try:
            self.audio_queue.put_nowait(data)
        except queue.Full:
            # Keep transcription near-live without ever blocking the shared
            # native camera packet reader (which would also freeze video).
            try:
                dropped = self.audio_queue.get_nowait()
            except queue.Empty:
                return
            with self.lock:
                self.audio_dropped_bytes += len(dropped)
            try:
                self.audio_queue.put_nowait(data)
            except queue.Full:
                pass

    def _write_audio(
        self, process: subprocess.Popen, stop_event: threading.Event
    ) -> None:
        if process.stdin is None:
            return
        while not stop_event.is_set() and process.poll() is None:
            try:
                data = self.audio_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if stop_event.is_set():
                return
            try:
                remaining = memoryview(data)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        return
                    with self.lock:
                        self.audio_written_bytes += written
                    remaining = remaining[written:]
            except (BrokenPipeError, OSError, ValueError):
                return

    def _read_events(self, process: subprocess.Popen) -> None:
        try:
            for raw in process.stdout:
                try:
                    event = json.loads(raw.decode(errors="replace"))
                except ValueError:
                    continue
                with self.lock:
                    self.last_event_at = time.time()
                    kind = event.get("type")
                    if kind == "status":
                        self.state = event.get("state", self.state)
                        self.model = event.get("model", self.model)
                        self.device = event.get("device", self.device)
                        self.chunk_seconds = float(
                            event.get("chunk_seconds", self.chunk_seconds)
                        )
                    elif kind == "transcript":
                        text = event.get("text", "")
                        duration = max(
                            0.0,
                            float(event.get("ended", 0.0)) - float(event.get("started", 0.0)),
                        )
                        rejection = transcript_rejection_reason(text, duration)
                        if rejection is None:
                            rejection = duplicate_transcript_reason(
                                text, self.recent_transcript_texts
                            )
                        if rejection is not None:
                            self.silence_events += 1
                            self.diagnostics.append(
                                f"discarded transcript ({rejection}): {str(text)[:120]}"
                            )
                            continue
                        self.seq += 1
                        event["id"] = self.seq
                        event["received_at"] = self.last_event_at
                        self.lines.append(event)
                        self.recent_transcript_texts.append(
                            normalize_transcript_text(text)
                        )
                        self.state = "running"
                        self.error = None
                        self.last_rms_db = event.get("rms_db")
                    elif kind == "silence":
                        self.state = "running"
                        self.silence_events += 1
                        self.last_rms_db = event.get("rms_db")
                    elif kind == "error":
                        self.state = "error"
                        self.error = event.get("error", "transcription worker failed")
        finally:
            code = process.wait()
            with self.lock:
                if self.process is process:
                    self.process = None
                    if self.state not in ("stopped", "error"):
                        self.state = "error"
                        detail = "\n".join(self.diagnostics)
                        self.error = detail[-1000:] if detail else f"worker exited {code}"

    def _read_diagnostics(self, process: subprocess.Popen) -> None:
        for raw in process.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                with self.lock:
                    self.diagnostics.append(line)

    def status_locked(self) -> dict:
        running = self.process is not None and self.process.poll() is None
        return {
            "state": self.state,
            "running": running,
            "model": self.model,
            "device": self.device,
            "seq": self.seq,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "audio_received_bytes": self.audio_received_bytes,
            "audio_written_bytes": self.audio_written_bytes,
            "audio_dropped_bytes": self.audio_dropped_bytes,
            "audio_seconds": round(self.audio_written_bytes / 32_000.0, 2),
            "chunk_seconds": self.chunk_seconds,
            "silence_events": self.silence_events,
            "last_rms_db": self.last_rms_db,
            "error": self.error,
            "lines": list(self.lines),
        }

    def status(self) -> dict:
        with self.lock:
            return self.status_locked()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "bodycam/1.0"
    protocol_version = "HTTP/1.1"
    library: Library
    streamer: "Streamer"
    transcriber: "Transcriber"

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        if "/api/" not in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def handle_one_request(self) -> None:
        # A viewer closing a tab resets a keep-alive connection, which is normal
        # here and not worth a traceback in the log.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # -- helpers ---------------------------------------------------------
    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status=status)

    def send_file(self, path: Path, content_type: str | None = None, download: bool = False) -> None:
        """Serve a file with range support so the player can seek."""
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error_json(404, "not found")
            return

        ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        partial = False

        header = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)", header)
        if match and size:
            raw_start, raw_end = match.groups()
            if raw_start:
                start = int(raw_start)
                end = int(raw_end) if raw_end else size - 1
            elif raw_end:  # suffix range: last N bytes
                start = max(0, size - int(raw_end))
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()

        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # player seeked away or closed the tab
                remaining -= len(chunk)

    def serve_mjpeg(self) -> None:
        """Stream frames as multipart/x-mixed-replace, which <img> renders natively."""
        boundary = "frameboundary"
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True  # response has no length; it ends when the socket does

        self.streamer.ensure_running()
        self.streamer.add_viewer()
        # Do not send a frame left over from an earlier source generation. The
        # producer will publish a current frame as soon as capture/replay starts.
        last_seq = self.streamer.seq
        try:
            while True:
                frame, seq = self.streamer.wait_for_frame(last_seq, timeout=2.0)
                if frame is None or seq == last_seq:
                    # No new frame -- e.g. the camera is unplugged. Poke the
                    # socket anyway: without a write, a viewer that has gone
                    # away is never noticed, viewers never drops back to zero,
                    # and the producer keeps thinking someone is watching.
                    # Whitespace between parts is legal in multipart.
                    self.wfile.write(b"\r\n")
                    continue
                last_seq = seq
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode()
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # viewer closed the tab
        finally:
            self.streamer.remove_viewer()

    def serve_pcm(self) -> None:
        """Continuously stream camera PCM for the outbound public publisher.

        This endpoint stays local to the Mac.  It is deliberately raw 16 kHz
        mono s16le; ``publish_worker.py`` frames it for the relay WebSocket,
        which lets the browser use Web Audio without involving the laptop mic.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-SamCam-Audio-Format", "s16le")
        self.send_header("X-SamCam-Audio-Rate", str(LIVE_AUDIO_SAMPLE_RATE))
        self.send_header("X-SamCam-Audio-Channels", str(LIVE_AUDIO_CHANNELS))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        self.streamer.ensure_running()
        self.streamer.add_audio_viewer()
        # Do not replay sound from before this connection. A reconnect should
        # always rejoin near live rather than sound like a delayed recording.
        last_seq = self.streamer.audio_seq
        try:
            while True:
                packets, sequence = self.streamer.wait_for_audio(last_seq, timeout=1.0)
                last_seq = sequence
                for packet in packets:
                    self.wfile.write(packet)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.streamer.remove_audio_viewer()

    def clip_from_path(self, suffix: str) -> Clip | None:
        clip_id = suffix.rsplit(".", 1)[0]
        if not SAFE_ID.match(clip_id):
            return None
        return self.library.get(clip_id)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/":
            self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
            return

        if path == "/api/status":
            self.send_json(self.library.status())
            return

        if path == "/api/clips":
            self.send_json({"clips": [c.to_json() for c in self.library.ordered()]})
            return

        if path == "/api/stream":
            self.send_json(self.streamer.status())
            return

        if path == "/api/transcript":
            self.send_json(self.transcriber.status())
            return

        if path.startswith("/api/analytics/"):
            clip_id = path[len("/api/analytics/"):]
            if not SAFE_ID.match(clip_id):
                self.send_error_json(400, "invalid clip id")
                return
            clip = self.library.get(clip_id)
            if clip is None:
                self.send_error_json(404, "unknown clip")
                return
            if not clip.mp4.exists():
                self.send_error_json(409, "video is still preparing")
                return
            try:
                self.send_json(analyze_clip(clip))
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.send_error_json(500, str(exc))
            return

        if path == "/stream.mjpg":
            self.serve_mjpeg()
            return

        if path == "/stream.pcm":
            self.serve_pcm()
            return

        for prefix, attr, ctype, download in (
            ("/media/", "mp4", "video/mp4", False),
            ("/thumb/", "thumb", "image/jpeg", False),
        ):
            if path.startswith(prefix):
                clip = self.clip_from_path(path[len(prefix):])
                if clip is None:
                    self.send_error_json(404, "unknown clip")
                    return
                target = getattr(clip, attr)
                if not target.exists():
                    self.send_error_json(409, f"still {clip.state}")
                    return
                self.send_file(target, ctype, download)
                return

        if path.startswith("/raw/"):
            clip = self.clip_from_path(path[len("/raw/"):])
            if clip is None or not clip.source.exists():
                self.send_error_json(404, "unknown clip")
                return
            ctype = mimetypes.guess_type(clip.source.name)[0] or "application/octet-stream"
            self.send_file(clip.source, ctype, download=True)
            return

        candidate = (STATIC / path.lstrip("/")).resolve()
        if candidate.is_file() and STATIC.resolve() in candidate.parents:
            self.send_file(candidate)
            return

        self.send_error_json(404, "not found")

def scanner_loop(library: Library, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            library.scan()
        except Exception as exc:  # noqa: BLE001 - the loop must not die
            print(f"  scan error: {exc}", file=sys.stderr)
        stop_event.wait(SCAN_INTERVAL)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    CACHE.mkdir(exist_ok=True)
    # Clean up a helper from an interrupted previous run before this process
    # begins accepting viewers.  That makes a normal restart a camera recovery
    # action instead of a second competing UVC client.
    reap_orphaned_native_helpers()
    # Bind before starting MLX or camera processes so a port/permission failure
    # cannot leave background workers behind.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True

    library = Library()
    library.scan()
    Handler.library = library
    transcriber = Transcriber()
    transcriber.start()
    streamer = Streamer(transcriber)
    streamer.ensure_running()
    Handler.transcriber = transcriber
    Handler.streamer = streamer
    stop_event = threading.Event()
    threading.Thread(
        target=scanner_loop, args=(library, stop_event), daemon=True
    ).start()

    camera = library.camera
    print()
    if camera:
        print(f"  camera   {camera.volume}  ({len(library.clips)} clips)")
    else:
        print("  camera   not found -- plug it in via USB and wait for it to mount")
    print(f"  serving  http://localhost:{port}")
    print("  ctrl-c to stop\n")

    shutting_down = threading.Event()

    def request_shutdown(signum, _frame) -> None:
        """Turn SIGTERM (including a Terminal/app restart) into cleanup."""
        if shutting_down.is_set():
            return
        shutting_down.set()
        print(f"\n  received {signal.Signals(signum).name}; stopping cleanly", file=sys.stderr)
        # HTTPServer.shutdown() must run outside serve_forever's thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye")
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        stop_event.set()
        streamer.stop()
        transcriber.stop()
        server.server_close()
        library.close()


if __name__ == "__main__":
    main()
