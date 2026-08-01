#!/usr/bin/env python3
"""Replace a completed archive soundtrack with a user-approved reference.

This tool preserves three independent artifacts in the session directory:

* ``recording.original.mp4``: the untouched original camera video/audio.
* ``audio.bodycam-original.m4a``: its original encoded AAC audio packets.
* ``audio.fixed-reference.m4a``: the approved replacement AAC packets.

``recording.mp4`` becomes the original camera video stream plus the approved
reference audio stream. There is no filtering, resampling, downmixing, or
audio re-encoding. Both audio copies and the final output are verified with
FFprobe SHA-256 packet hashes before the public playback file is replaced.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from restore_archive_audio import RestorationError, encoded_audio_packet_hashes
from sync_archive_playback_audio import encoded_video_packet_hashes


class ReferenceAudioError(RuntimeError):
    """A reference track did not pass the non-destructive archive checks."""


def required(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise ReferenceAudioError(f"{binary} is required (try: brew install ffmpeg)")
    return path


def run(command: list[str], *, timeout: float = 600) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-1_000:]
        raise ReferenceAudioError(detail or "FFmpeg did not produce media")


def extract_audio_command(ffmpeg: str, source: Path, destination: Path) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(source),
        "-map", "0:a:0", "-c:a", "copy", "-movflags", "+faststart", str(destination),
    ]


def remux_command(ffmpeg: str, camera: Path, reference: Path, destination: Path) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-i", str(camera), "-i", str(reference),
        "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "0", "-map_chapters", "0",
        "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", str(destination),
    ]


def audio_hashes(ffprobe: str, path: Path) -> tuple[str, ...]:
    try:
        return encoded_audio_packet_hashes(ffprobe, path)
    except RestorationError as exc:
        raise ReferenceAudioError(str(exc)) from exc


def apply_reference_audio(
    session_directory: Path,
    reference: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    """Atomically replace playback audio while retaining exact comparison copies."""
    directory = session_directory.resolve()
    camera = directory / "recording.original.mp4"
    playback = directory / "recording.mp4"
    reference = reference.resolve()
    if not camera.is_file() or camera.stat().st_size <= 0:
        raise ReferenceAudioError(f"missing original camera recording: {camera}")
    if not reference.is_file() or reference.stat().st_size <= 0:
        raise ReferenceAudioError(f"missing reference media: {reference}")

    original_audio = directory / "audio.bodycam-original.m4a"
    fixed_audio = directory / "audio.fixed-reference.m4a"
    original_temporary = original_audio.with_name(f".{original_audio.stem}.{uuid.uuid4().hex}.m4a")
    fixed_temporary = fixed_audio.with_name(f".{fixed_audio.stem}.{uuid.uuid4().hex}.m4a")
    playback_temporary = playback.with_name(f".{playback.stem}.{uuid.uuid4().hex}.mp4")
    try:
        original_hashes = audio_hashes(ffprobe, camera)
        fixed_hashes = audio_hashes(ffprobe, reference)
        camera_video = encoded_video_packet_hashes(ffprobe, camera)
        run(extract_audio_command(ffmpeg, camera, original_temporary))
        run(extract_audio_command(ffmpeg, reference, fixed_temporary))
        if audio_hashes(ffprobe, original_temporary) != original_hashes:
            raise ReferenceAudioError("original body-camera audio copy changed encoded packets")
        if audio_hashes(ffprobe, fixed_temporary) != fixed_hashes:
            raise ReferenceAudioError("fixed reference audio copy changed encoded packets")
        run(remux_command(ffmpeg, camera, reference, playback_temporary))
        if audio_hashes(ffprobe, playback_temporary) != fixed_hashes:
            raise ReferenceAudioError("final playback does not use the approved reference audio packets")
        if encoded_video_packet_hashes(ffprobe, playback_temporary) != camera_video:
            raise ReferenceAudioError("final playback changed encoded camera video packets")
        original_temporary.replace(original_audio)
        fixed_temporary.replace(fixed_audio)
        playback_temporary.replace(playback)
    finally:
        original_temporary.unlink(missing_ok=True)
        fixed_temporary.unlink(missing_ok=True)
        playback_temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_directory", type=Path)
    parser.add_argument("reference", type=Path, help="approved media with the replacement audio in its first audio stream")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        apply_reference_audio(args.session_directory, args.reference, ffmpeg=required("ffmpeg"), ffprobe=required("ffprobe"))
        print(f"Updated archive playback: {args.session_directory}")
    except (ReferenceAudioError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Reference-audio replacement failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
