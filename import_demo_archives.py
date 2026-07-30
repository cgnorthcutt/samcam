#!/usr/bin/env python3
"""Add the supplied Meta Oakley Vanguard demos to the Sam Cam archive.

The browser-ready H.264/AAC copies live under ``.cache``.  Their original
HEVC MOVs retain the authoritative QuickTime capture dates, so the importer
uses those dates when available and otherwise uses the verified dates in the
seed data.  It never assigns import-time timestamps to a recording.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
ARCHIVES = HERE / "archives"
SOURCES = HERE / "sources"
DOWNLOADS = Path.home() / "Downloads"


@dataclass(frozen=True)
class Demo:
    session_id: str
    source_label: str
    capture_device: str
    converted_path: Path
    original_filename: str
    # This is the QuickTime ``com.apple.quicktime.creationdate`` value read
    # from the supplied original MOV.  It keeps public imports correct even
    # when the large source MOV is not checked into this repository.
    verified_recorded_at: str

    @property
    def verified_timestamp(self) -> float:
        return parse_timestamp(self.verified_recorded_at)

    def original_candidates(self) -> tuple[Path, ...]:
        return (
            SOURCES / self.original_filename,
            DOWNLOADS / self.original_filename,
        )


DEMOS = (
    Demo(
        session_id="Curtis-demo-field-walk-20260728",
        source_label="Demo · Field walkthrough",
        capture_device="Meta Oakley Vanguard AI Glasses",
        converted_path=HERE / ".cache" / "mcp_video-289_singular_display-1785281112-170762099.mp4",
        original_filename="mcp_video-289_singular_display.mov",
        verified_recorded_at="2026-07-25T15:06:39-07:00",
    ),
    Demo(
        session_id="Curtis-demo-drive-20260728",
        source_label="Demo · Driver view",
        capture_device="Meta Oakley Vanguard AI Glasses",
        converted_path=HERE / ".cache" / "IMG_3095-1785279932-141342585.mp4",
        original_filename="IMG_3095.MOV",
        verified_recorded_at="2026-07-25T22:36:50Z",
    ),
)


def parse_timestamp(value: str) -> float:
    """Parse an ISO 8601 media timestamp into a UTC epoch."""
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError(f"media timestamp has no timezone: {value!r}")
    return parsed.timestamp()


def recorded_at_from_tags(tags: dict[str, object]) -> float | None:
    """Prefer the device-authored QuickTime capture timestamp.

    ``creation_time`` is often rewritten while exporting or transcoding a
    video.  The Meta MOVs preserve their capture time in the QuickTime
    creation-date atom, which is therefore deliberately checked first.
    """
    normalized = {str(key).casefold(): str(value) for key, value in tags.items()}
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        value = normalized.get(key)
        if not value:
            continue
        try:
            return parse_timestamp(value)
        except ValueError:
            continue
    return None


def embedded_recorded_at(path: Path) -> float | None:
    """Read capture metadata with ffprobe without trusting file mtime."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format_tags=creation_time,com.apple.quicktime.creationdate",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    tags = payload.get("format", {}).get("tags", {})
    return recorded_at_from_tags(tags) if isinstance(tags, dict) else None


def demo_recorded_at(demo: Demo) -> float:
    """Resolve a demo capture date, using original media before seed fallback."""
    for candidate in demo.original_candidates():
        if candidate.is_file():
            embedded = embedded_recorded_at(candidate)
            if embedded is not None:
                return embedded
            # File creation time is the last fallback only for an original
            # MOV with no metadata; never use the converted cache timestamp.
            stat = candidate.stat()
            return getattr(stat, "st_birthtime", stat.st_mtime)
    return demo.verified_timestamp


def duration_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise ValueError(f"{path.name} has no duration")
    return duration


def first_original_media(demo: Demo) -> Path | None:
    """Return the supplied device capture when it is available locally.

    The browser-compatible cache intentionally contains H.264 video, but it
    can have a lower-bitrate AAC track than the supplied HEVC MOV.  The MOV is
    the authoritative capture, so we use its audio bitstream without
    re-encoding whenever it is present.
    """
    return next((path for path in demo.original_candidates() if path.is_file()), None)


def merge_browser_video_with_original_audio(
    browser_video: Path,
    original_capture: Path,
    destination: Path,
) -> None:
    """Create a browser-safe MP4 while preserving original AAC packets.

    The first input supplies the existing H.264 video; the device MOV supplies
    its original audio.  ``-c copy`` is deliberate: it avoids applying the
    body-camera speech-repair pipeline, changing channel layout, or encoding a
    second lossy AAC generation to a healthy stereo recording.
    """
    temporary = destination.with_name(f".{destination.stem}.audio-source.part{destination.suffix}")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to preserve original demo audio")
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(browser_video),
        "-i",
        str(original_capture),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise RuntimeError(detail or "ffmpeg could not merge the original demo audio")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def materialize_demo_recording(demo: Demo, destination: Path) -> None:
    """Create the public demo recording without degrading device audio."""
    original = first_original_media(demo)
    if original is None:
        # The repository can still be cloned without 100+ MB MOV sources.  In
        # that case the known browser cache remains a playable fallback; the
        # caller never claims that fallback is a lossless original-audio copy.
        shutil.copyfile(demo.converted_path, destination)
        return
    merge_browser_video_with_original_audio(demo.converted_path, original, destination)


def write_metadata(path: Path, metadata: dict[str, object]) -> None:
    serialized = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    if path.exists() and path.read_text() == serialized:
        return
    temporary = path.with_name("metadata.json.part")
    temporary.write_text(serialized)
    temporary.replace(path)


def import_demo(demo: Demo, *, replace_media: bool = False) -> None:
    source = demo.converted_path
    if not source.exists():
        raise FileNotFoundError(f"missing converted demo video: {source}")
    session_dir = ARCHIVES / demo.session_id
    recording = session_dir / "recording.mp4"
    duration = duration_seconds(source)
    recorded_at = demo_recorded_at(demo)
    session_dir.mkdir(parents=True, exist_ok=True)
    if replace_media or not recording.exists():
        materialize_demo_recording(demo, recording)
    metadata = {
        "session_id": demo.session_id,
        "worker_name": "Curtis",
        "source": demo.source_label,
        "capture_device": demo.capture_device,
        "started_at": recorded_at,
        "ended_at": recorded_at + duration,
        "status": "complete",
    }
    write_metadata(session_dir / "metadata.json", metadata)
    (session_dir / "transcript.jsonl").touch(exist_ok=True)
    print(f"imported {demo.session_id}: {duration:.1f}s H.264/AAC MP4")


def main(argv: list[str] | None = None) -> None:
    arguments = argv if argv is not None else sys.argv[1:]
    replace_media = "--replace-media" in arguments
    unexpected = [argument for argument in arguments if argument != "--replace-media"]
    if unexpected:
        raise SystemExit(f"unknown argument(s): {' '.join(unexpected)}")
    for demo in DEMOS:
        import_demo(demo, replace_media=replace_media)


if __name__ == "__main__":
    main()
