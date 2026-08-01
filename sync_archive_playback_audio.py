#!/usr/bin/env python3
"""Make archive playback MP4s use the retained original camera audio.

The live publisher keeps ``recording.original.mp4`` alongside the normal
archive playback file. This utility repairs legacy playback files that were
created before that contract existed. It copies the *encoded* audio packets
from the retained original recording; it never decodes, filters, resamples,
downmixes, or re-encodes camera audio.

The operation is safe to run repeatedly. Each target is written to a unique
temporary MP4, validated with FFprobe packet hashes, and atomically moved into
place only after the copied audio is proven identical to the retained source.

Examples:

    python3 sync_archive_playback_audio.py archives
    python3 sync_archive_playback_audio.py archives --session Curtis-20260730T161648Z-b54a5006
    python3 sync_archive_playback_audio.py archives --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterable

from restore_archive_audio import (
    RestorationError,
    encoded_audio_packet_hashes,
    probe_media,
    stream_list,
)


class PlaybackAudioSyncError(RuntimeError):
    """A playback MP4 could not be safely repaired."""


def required_binary(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise PlaybackAudioSyncError(f"{binary} is required (try: brew install ffmpeg)")
    return path


def encoded_video_packet_hashes(ffprobe: str, path: Path) -> tuple[str, ...]:
    """Hash encoded video packets to prove the image stream was copied intact."""
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=data_hash",
            "-show_data_hash",
            "sha256",
            "-of",
            "compact=p=0:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise PlaybackAudioSyncError(detail or f"FFprobe could not inspect video packets: {path}")
    hashes = tuple(
        line.split("SHA256:", 1)[1].strip()
        for line in completed.stdout.splitlines()
        if "SHA256:" in line
    )
    if not hashes:
        raise PlaybackAudioSyncError(f"playback MP4 has no video packets: {path}")
    return hashes


def has_audio_stream(path: Path, ffprobe: str) -> bool:
    """Return whether the MP4 contains a first audio stream to preserve."""
    try:
        return bool(stream_list(probe_media(ffprobe, path), "audio"))
    except RestorationError as exc:
        raise PlaybackAudioSyncError(str(exc)) from exc


def sync_command(ffmpeg: str, playback: Path, original: Path, destination: Path) -> list[str]:
    """Preserve the playback video and replace only its audio packet stream."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(playback),
        "-i",
        str(original),
        "-map",
        "0:v?",
        "-map",
        "1:a:0",
        "-map",
        "0:s?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]


def sync_playback_audio(
    playback: Path,
    original: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    dry_run: bool = False,
) -> str:
    """Return ``unchanged`` or ``repaired`` after a packet-preserving sync."""
    playback = playback.resolve()
    original = original.resolve()
    if not playback.is_file() or playback.stat().st_size <= 0:
        raise PlaybackAudioSyncError(f"playback MP4 is missing or empty: {playback}")
    if not original.is_file() or original.stat().st_size <= 0:
        raise PlaybackAudioSyncError(f"original camera MP4 is missing or empty: {original}")

    # Some early silent captures predate audio transport entirely. Never
    # replace a playable MP4 with an empty audio track just to make filenames
    # agree; report it clearly and leave the archived media untouched.
    if not has_audio_stream(original, ffprobe):
        return "no-original-audio"
    playback_has_audio = has_audio_stream(playback, ffprobe)
    if not playback_has_audio and dry_run:
        return "would-add-audio"

    try:
        original_packets = encoded_audio_packet_hashes(ffprobe, original)
        playback_packets = encoded_audio_packet_hashes(ffprobe, playback) if playback_has_audio else ()
    except RestorationError as exc:
        raise PlaybackAudioSyncError(str(exc)) from exc
    if playback_packets == original_packets:
        return "unchanged"
    if dry_run:
        return "would-repair"

    source_video = encoded_video_packet_hashes(ffprobe, playback)
    temporary = playback.with_name(f".{playback.stem}.{uuid.uuid4().hex}.mp4")
    try:
        completed = subprocess.run(
            sync_command(ffmpeg, playback, original, temporary),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            detail = (completed.stderr or completed.stdout).strip()[-1_000:]
            raise PlaybackAudioSyncError(detail or "FFmpeg did not create a repaired playback MP4")
        try:
            repaired_packets = encoded_audio_packet_hashes(ffprobe, temporary)
        except RestorationError as exc:
            raise PlaybackAudioSyncError(str(exc)) from exc
        if repaired_packets != original_packets:
            raise PlaybackAudioSyncError("repaired playback changed original encoded audio packets")
        if encoded_video_packet_hashes(ffprobe, temporary) != source_video:
            raise PlaybackAudioSyncError("repaired playback changed encoded video packets")
        temporary.replace(playback)
        return "repaired"
    finally:
        temporary.unlink(missing_ok=True)


def session_directories(root: Path, selected: Iterable[str]) -> list[Path]:
    selected_ids = {value.strip() for value in selected if value.strip()}
    if selected_ids:
        return [root / session_id for session_id in sorted(selected_ids)]
    return sorted(path for path in root.iterdir() if path.is_dir())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_root", type=Path, help="directory containing archive session folders")
    parser.add_argument("--session", action="append", default=[], help="repair only this session ID (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="report divergent sessions without writing files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.archive_root.expanduser().resolve()
    if not root.is_dir():
        print(f"Archive root does not exist: {root}", file=sys.stderr)
        return 2
    try:
        ffmpeg = required_binary("ffmpeg")
        ffprobe = required_binary("ffprobe")
        counts = {
            "unchanged": 0,
            "repaired": 0,
            "would-repair": 0,
            "would-add-audio": 0,
            "no-original-audio": 0,
            "skipped": 0,
        }
        for session_dir in session_directories(root, args.session):
            playback = session_dir / "recording.mp4"
            original = session_dir / "recording.original.mp4"
            if not playback.exists() or not original.exists():
                counts["skipped"] += 1
                continue
            result = sync_playback_audio(
                playback,
                original,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                dry_run=args.dry_run,
            )
            counts[result] += 1
            print(f"{session_dir.name}: {result}")
        print(
            "Summary: " + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
    except (PlaybackAudioSyncError, OSError) as exc:
        print(f"Playback audio sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
