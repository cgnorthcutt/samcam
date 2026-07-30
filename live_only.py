#!/usr/bin/env python3
"""Generalplus body-camera-only live MJPEG server for macOS.

The service intentionally refuses every source except a USB device that:

1. is from Generalplus (USB vendor 1b3f),
2. exposes a USB Video Class interface (class 14), and
3. appears in AVFoundation after that USB personality is present.

Before publishing video, FFmpeg decodes a short sample to framemd5 and the
service requires changing frame hashes.  It never falls back to the Mac camera,
an iPhone, screen capture, a clip, or another external camera.

Requirements: macOS, Python 3.9+, and ffmpeg on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import BinaryIO
from urllib.parse import urlparse


GENERALPLUS_VID = 0x1B3F
USB_CLASS_VIDEO = 14
USB_CLASS_STORAGE = 8
SOI = b"\xff\xd8\xff"
EOI = b"\xff\xd9"
START_TIMEOUT = 10.0
STALL_TIMEOUT = 5.0
POLL_INTERVAL = 0.5

BUILTIN_OR_VIRTUAL_HINTS = (
    "macbook",
    "facetime",
    "desk view",
    "iphone",
    "ipad",
    "continuity",
    "capture screen",
    "screen capture",
    "obs",
)
GENERALPLUS_NAME_HINTS = ("general", "uvc")

# Default first lets AVFoundation choose the device's native mode.  The
# explicit profiles cover the formats most often exposed by this camera family.
PROFILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("default", ()),
    ("640x480@30", ("-framerate", "30", "-video_size", "640x480")),
    ("1280x720@30", ("-framerate", "30", "-video_size", "1280x720")),
    ("1920x1080@30", ("-framerate", "30", "-video_size", "1920x1080")),
    ("640x480@10", ("-framerate", "10", "-video_size", "640x480")),
    ("1280x720@10", ("-framerate", "10", "-video_size", "1280x720")),
)


@dataclass(frozen=True)
class USBInterface:
    vid: int
    pid: int
    interface_class: int
    location: int
    name: str

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def location_hex(self) -> str:
        return f"0x{self.location:08x}"


@dataclass(frozen=True)
class CaptureDevice:
    index: int
    name: str


@dataclass(frozen=True)
class SelectedDevice:
    index: int
    name: str
    selector: str
    selected_by: str
    usb_id: str
    location: str


class LiveState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.frame: bytes | None = None
        self.sequence = 0
        self.generation = 0
        self.live = False
        self.gate = "waiting_for_generalplus_uvc"
        self.detail = (
            "Camera is not in UVC mode. With it connected as USB storage, "
            "short-press ON/OFF and wait for GENERAL-UVC."
        )
        self.device: SelectedDevice | None = None
        self.profile: str | None = None
        self.frame_hashes: list[str] = []
        self.last_frame_at: float | None = None
        self.ffmpeg_error: str | None = None
        self.started_at = time.time()

    def update(self, **values: object) -> None:
        with self.condition:
            for key, value in values.items():
                setattr(self, key, value)
            self.condition.notify_all()

    def reset_stream(self, gate: str, detail: str, error: str | None = None) -> None:
        with self.condition:
            self.live = False
            self.frame = None
            self.generation += 1
            self.gate = gate
            self.detail = detail
            self.profile = None
            self.frame_hashes = []
            self.last_frame_at = None
            self.ffmpeg_error = error
            self.condition.notify_all()

    def publish(self, frame: bytes) -> None:
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.live = True
            self.gate = "live"
            self.detail = "Changing Generalplus frames are streaming."
            self.last_frame_at = time.time()
            self.condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        with self.condition:
            return {
                "live": self.live,
                "gate": self.gate,
                "detail": self.detail,
                "device": asdict(self.device) if self.device else None,
                "profile": self.profile,
                "sequence": self.sequence,
                "framemd5_sample": list(self.frame_hashes),
                "last_frame_at": self.last_frame_at,
                "ffmpeg_error": self.ffmpeg_error,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "stream_url": "/stream.mjpg",
            }


def command_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"error: {name} is required (try: brew install ffmpeg)")
    return path


def run_text(command: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _integer_field(blob: str, key: str) -> int | None:
    match = re.search(rf'"{re.escape(key)}"\s*=\s*(\d+)', blob)
    return int(match.group(1)) if match else None


def _string_field(blob: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*=\s*"([^"]*)"', blob)
    return match.group(1) if match else ""


def parse_usb_interfaces(blob: str) -> list[USBInterface]:
    """Parse root interface properties and older nested USB dictionaries.

    Before macOS claims this camera, ioreg commonly exposes a nested
    ``USB Device Info`` dictionary. After UVCAssistant claims the class-14
    personality, the same fields are direct properties of the root interface
    instead. Parse both shapes so the mode transition cannot hide the device.
    """
    found: set[USBInterface] = set()
    bodies = re.findall(r'"USB Device Info"\s*=\s*\{([^}]*)\}', blob)

    root: list[str] = []
    for line in blob.splitlines():
        if line.startswith("+-o "):
            if root:
                bodies.append("\n".join(root))
            root = [line] if "<class IOUSBHostInterface" in line else []
        elif root:
            root.append(line)
    if root:
        bodies.append("\n".join(root))

    for body in bodies:
        vid = _integer_field(body, "idVendor")
        pid = _integer_field(body, "idProduct")
        interface_class = _integer_field(body, "bInterfaceClass")
        location = _integer_field(body, "locationID")
        if None in (vid, pid, interface_class, location):
            continue
        found.add(
            USBInterface(
                vid=vid,
                pid=pid,
                interface_class=interface_class,
                location=location,
                name=_string_field(body, "USB Product Name") or "unknown",
            )
        )
    return sorted(
        found,
        key=lambda item: (item.location, item.vid, item.pid, item.interface_class),
    )


def usb_interfaces() -> list[USBInterface]:
    proc = run_text(
        ["ioreg", "-r", "-c", "IOUSBHostInterface", "-l", "-w0"],
        timeout=10,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "ioreg failed")
    return parse_usb_interfaces(proc.stdout)


def parse_capture_devices(stderr: str) -> list[CaptureDevice]:
    devices: list[CaptureDevice] = []
    in_video = False
    for line in stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            in_video = False
            continue
        match = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if in_video and match:
            devices.append(CaptureDevice(int(match.group(1)), match.group(2)))
    return devices


def capture_devices(ffmpeg: str) -> tuple[list[CaptureDevice], str]:
    proc = run_text(
        [
            ffmpeg,
            "-hide_banner",
            "-f",
            "avfoundation",
            "-list_devices",
            "true",
            "-i",
            "",
        ]
    )
    return parse_capture_devices(proc.stderr), proc.stderr.strip()


def excluded_capture(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in BUILTIN_OR_VIRTUAL_HINTS)


def select_capture_device(
    devices: list[CaptureDevice],
    baseline_names: Counter[str],
    bodycam_usb: USBInterface,
) -> SelectedDevice | None:
    """Select only a Generalplus-correlated AVFoundation device.

    A unique vendor-ish name is safe because the USB layer independently proves
    a Generalplus UVC interface exists.  Generic names are accepted only when
    they are newly added and are the sole eligible arrival.
    """
    eligible = [device for device in devices if not excluded_capture(device.name)]
    counts = Counter(device.name for device in devices)
    preferred = [
        device
        for device in eligible
        if any(hint in device.name.lower() for hint in GENERALPLUS_NAME_HINTS)
    ]
    newly_arrived: list[CaptureDevice] = []
    seen_now: Counter[str] = Counter()
    for device in eligible:
        seen_now[device.name] += 1
        if seen_now[device.name] > baseline_names[device.name]:
            newly_arrived.append(device)

    candidates = preferred or newly_arrived
    if len(candidates) != 1:
        return None

    chosen = candidates[0]
    # Names survive index reordering and reconnects.  Use a numeric index only
    # if the name is duplicated, and only from this just-refreshed device list.
    if counts[chosen.name] == 1:
        selector, selected_by = chosen.name, "unique_name"
    else:
        selector, selected_by = str(chosen.index), "fresh_index_for_duplicate_name"
    return SelectedDevice(
        index=chosen.index,
        name=chosen.name,
        selector=selector,
        selected_by=selected_by,
        usb_id=bodycam_usb.usb_id,
        location=bodycam_usb.location_hex,
    )


def avfoundation_input(
    ffmpeg: str,
    selector: str,
    input_options: tuple[str, ...],
) -> list[str]:
    # All AVFoundation input options must precede -i. Audio is explicitly none,
    # avoiding microphone permission and broken Generalplus audio descriptors.
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-fflags",
        "nobuffer",
        "-f",
        "avfoundation",
        *input_options,
        "-i",
        f"{selector}:none",
    ]


def parse_framemd5(stdout: str) -> list[str]:
    hashes: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("#"):
            continue
        match = re.search(r",\s*([0-9a-fA-F]{32})\s*$", line)
        if match:
            hashes.append(match.group(1).lower())
    return hashes


def permission_diagnosis(stderr: str) -> str | None:
    lowered = stderr.lower()
    if any(
        phrase in lowered
        for phrase in (
            "not authorized",
            "permission denied",
            "access denied",
            "user denied",
            "authorization status",
        )
    ):
        return (
            "macOS denied camera access. Enable Camera access for the app that "
            "launched this process in System Settings > Privacy & Security > Camera, "
            "then restart live_only.py."
        )
    return None


def compact_error(stderr: str, limit: int = 8) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return "\n".join(lines[-limit:]) or "FFmpeg returned no diagnostic text."


def prove_changing_frames(
    ffmpeg: str,
    selected: SelectedDevice,
    input_options: tuple[str, ...],
) -> tuple[bool, list[str], str]:
    """Use decoded-frame framemd5 output to reject no-frame/frozen sources."""
    command = [
        *avfoundation_input(ffmpeg, selected.selector, input_options),
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        "12",
        "-f",
        "framemd5",
        "pipe:1",
    ]
    try:
        proc = run_text(command, timeout=12)
    except subprocess.TimeoutExpired as exc:
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return False, [], f"first-frame/change probe timed out; {compact_error(stderr)}"

    hashes = parse_framemd5(proc.stdout)
    unique = len(set(hashes))
    if len(hashes) >= 3 and unique >= 2:
        return True, hashes, ""

    permission = permission_diagnosis(proc.stderr)
    if permission:
        return False, hashes, permission
    if hashes:
        return (
            False,
            hashes,
            f"received {len(hashes)} decoded frames but all hashes were identical; "
            "move the camera or wave a hand in front of the lens while it retries",
        )
    return False, hashes, f"no decoded frames; {compact_error(proc.stderr)}"


def jpeg_frames(stream: BinaryIO):
    buffer = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        buffer.extend(chunk)
        while True:
            start = buffer.find(SOI)
            if start < 0:
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


def terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            try:
                stream.close()
            except OSError:
                pass


def bodycam_uvc_at_known_location(
    interfaces: list[USBInterface],
    known_storage_locations: set[int],
) -> USBInterface | None:
    candidates = [
        item
        for item in interfaces
        if item.vid == GENERALPLUS_VID and item.interface_class == USB_CLASS_VIDEO
    ]
    if known_storage_locations:
        candidates = [
            item for item in candidates if item.location in known_storage_locations
        ]
    return candidates[0] if candidates else None


def stream_profile(
    ffmpeg: str,
    selected: SelectedDevice,
    input_options: tuple[str, ...],
    state: LiveState,
    stop: threading.Event,
    known_storage_locations: set[int],
) -> str:
    command = [
        *avfoundation_input(ffmpeg, selected.selector, input_options),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "mjpeg",
        "-q:v",
        "5",
        "-flush_packets",
        "1",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    frames: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
    errors: list[str] = []

    def read_output() -> None:
        assert proc.stdout is not None
        try:
            for frame in jpeg_frames(proc.stdout):
                try:
                    frames.put_nowait(frame)
                except queue.Full:
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

    def read_errors() -> None:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            if line:
                errors.append(line)
                del errors[:-30]

    threading.Thread(target=read_output, daemon=True).start()
    threading.Thread(target=read_errors, daemon=True).start()
    state.reset_stream("starting_stream", "Changing frames proved; starting MJPEG.")
    last_frame = time.monotonic()
    published = 0
    try:
        while not stop.is_set():
            if bodycam_uvc_at_known_location(usb_interfaces(), known_storage_locations) is None:
                return "Generalplus UVC disconnected or changed USB location."
            try:
                frame = frames.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                timeout = STALL_TIMEOUT if published else START_TIMEOUT
                if time.monotonic() - last_frame > timeout:
                    return f"FFmpeg capture stalled for {timeout:.0f} seconds."
                continue
            if frame is None:
                break
            state.publish(frame)
            published += 1
            last_frame = time.monotonic()
        if stop.is_set():
            return "Service stopped."
        return compact_error("\n".join(errors))
    finally:
        terminate_process(proc)


def supervisor(ffmpeg: str, state: LiveState, stop: threading.Event) -> None:
    initial_devices, initial_listing = capture_devices(ffmpeg)
    baseline_names = Counter(device.name for device in initial_devices)
    known_storage_locations: set[int] = set()
    announced_usb: tuple[int, int] | None = None
    retry_delay = 0.5

    while not stop.is_set():
        try:
            interfaces = usb_interfaces()
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            state.reset_stream("usb_error", str(exc), str(exc))
            stop.wait(1)
            continue

        for item in interfaces:
            if (
                item.vid == GENERALPLUS_VID
                and item.interface_class == USB_CLASS_STORAGE
            ):
                known_storage_locations.add(item.location)

        bodycam_usb = bodycam_uvc_at_known_location(
            interfaces, known_storage_locations
        )
        if bodycam_usb is None:
            state.device = None
            state.reset_stream(
                "waiting_for_generalplus_uvc",
                "Gate 1/4: connect as storage, then short-press ON/OFF. "
                "Waiting for vendor 1b3f with USB interface class 14.",
            )
            stop.wait(POLL_INTERVAL)
            continue

        usb_key = (bodycam_usb.pid, bodycam_usb.location)
        if usb_key != announced_usb:
            print(
                f"gate 1/4 USB: Generalplus {bodycam_usb.usb_id}, "
                f"class 14, location {bodycam_usb.location_hex}",
                flush=True,
            )
            announced_usb = usb_key
        state.update(
            gate="waiting_for_avfoundation",
            detail=(
                f"Gate 2/4: USB {bodycam_usb.usb_id} is UVC; waiting for "
                "its AVFoundation capture device."
            ),
        )

        try:
            devices, listing = capture_devices(ffmpeg)
        except (subprocess.SubprocessError, OSError) as exc:
            state.update(ffmpeg_error=str(exc))
            stop.wait(retry_delay)
            continue

        selected = select_capture_device(devices, baseline_names, bodycam_usb)
        if selected is None:
            names = [device.name for device in devices]
            detail = (
                "USB class 14 is present, but no unambiguous newly arrived "
                f"AVFoundation device is available. Video devices: {names or 'none'}. "
                "This points to macOS rejecting the UVC descriptors, or camera "
                "permission if the device appears only after access is granted."
            )
            permission = permission_diagnosis(listing or initial_listing)
            state.update(
                gate="waiting_for_avfoundation",
                detail=permission or detail,
                ffmpeg_error=compact_error(listing) if listing else None,
            )
            stop.wait(retry_delay)
            retry_delay = min(retry_delay * 1.5, 5.0)
            continue

        state.update(
            device=selected,
            gate="proving_changing_frames",
            detail=(
                f"Gate 3/4: selected {selected.name!r} by {selected.selected_by}; "
                "checking decoded framemd5 values."
            ),
            ffmpeg_error=None,
        )
        print(
            f"gate 2/4 AVFoundation: {selected.name!r} "
            f"(selector={selected.selector!r}, {selected.selected_by})",
            flush=True,
        )

        profile_failures: list[str] = []
        chosen: tuple[str, tuple[str, ...], list[str]] | None = None
        for label, options in PROFILES:
            if stop.is_set():
                return
            current_usb = bodycam_uvc_at_known_location(
                usb_interfaces(), known_storage_locations
            )
            if current_usb is None:
                break

            # Refresh the index/name immediately before each open. Names are
            # stable; duplicate-name numeric indexes are deliberately ephemeral.
            current_devices, _ = capture_devices(ffmpeg)
            refreshed = select_capture_device(
                current_devices, baseline_names, current_usb
            )
            if refreshed is None:
                profile_failures.append(f"{label}: device became ambiguous")
                break
            selected = refreshed
            state.update(device=selected, profile=label)

            ok, hashes, error = prove_changing_frames(
                ffmpeg, selected, options
            )
            if ok:
                chosen = (label, options, hashes)
                break
            profile_failures.append(f"{label}: {error}")
            state.update(ffmpeg_error=error, frame_hashes=hashes)

        if chosen is None:
            error = "\n".join(profile_failures[-6:]) or "UVC disconnected."
            state.reset_stream(
                "capture_probe_failed",
                "USB and AVFoundation succeeded, but changing decoded frames "
                "have not yet been proved. Retrying the format matrix.",
                error,
            )
            stop.wait(retry_delay)
            retry_delay = min(retry_delay * 1.5, 5.0)
            continue

        label, options, hashes = chosen
        state.update(
            device=selected,
            profile=label,
            frame_hashes=hashes[:12],
            gate="changing_frames_proved",
            detail=(
                f"Gate 3/4 passed: {len(set(hashes))} distinct decoded frame "
                f"hashes across {len(hashes)} frames."
            ),
            ffmpeg_error=None,
        )
        print(
            f"gate 3/4 framemd5: {len(set(hashes))} distinct hashes "
            f"from {len(hashes)} frames using {label}",
            flush=True,
        )
        retry_delay = 0.5
        reason = stream_profile(
            ffmpeg,
            selected,
            options,
            state,
            stop,
            known_storage_locations,
        )
        if state.sequence:
            print("gate 4/4 HTTP: live Generalplus MJPEG is publishing", flush=True)
        state.reset_stream("capture_ended", reason, reason)
        stop.wait(retry_delay)


def make_handler(state: LiveState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GeneralplusLive/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"http: {fmt % args}", file=sys.stderr)

        def send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlparse(self.path).path
            if path == "/status.json":
                self.send_json(200, state.snapshot())
                return
            if path == "/":
                self.send_json(
                    200,
                    {
                        "service": "Generalplus body-camera live stream",
                        "stream": "/stream.mjpg",
                        "status": "/status.json",
                        "rule": "No laptop, iPhone, screen, clip, or generic webcam fallback.",
                    },
                )
                return
            if path != "/stream.mjpg":
                self.send_json(404, {"error": "not found"})
                return

            deadline = time.monotonic() + START_TIMEOUT
            with state.condition:
                while (
                    (not state.live or state.frame is None)
                    and time.monotonic() < deadline
                ):
                    state.condition.wait(
                        min(0.5, max(0.0, deadline - time.monotonic()))
                    )
                if not state.live or state.frame is None:
                    self.send_json(503, state.snapshot())
                    return
                generation = state.generation
                sequence = state.sequence - 1

            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            try:
                while True:
                    with state.condition:
                        if state.generation != generation or not state.live:
                            return
                        if state.sequence == sequence:
                            state.condition.wait(2)
                            continue
                        frame = state.frame
                        sequence = state.sequence
                    if frame is None:
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    )
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if sys.platform != "darwin":
        raise SystemExit("error: live_only.py requires macOS AVFoundation")
    ffmpeg = command_path("ffmpeg")
    command_path("ioreg")

    state = LiveState()
    stop = threading.Event()
    worker = threading.Thread(
        target=supervisor, args=(ffmpeg, state, stop), daemon=True
    )
    worker.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print(f"Generalplus-only live service: http://{args.host}:{args.port}", flush=True)
    print(f"MJPEG endpoint: http://{args.host}:{args.port}/stream.mjpg", flush=True)
    print("No fallback cameras or saved videos are permitted.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
