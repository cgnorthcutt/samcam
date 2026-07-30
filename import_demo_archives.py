#!/usr/bin/env python3
"""Add the two supplied browser-ready demo videos to the Sam Cam archive.

The local server has already transcoded the original HEVC MOVs into H.264/AAC
MP4 files under ``.cache``.  This importer makes durable archive sessions from
those copies; the normal publisher then uploads the single MP4 plus its
video-derived analytics to the public relay.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
ARCHIVES = HERE / "archives"
DEMOS = (
    (
        "Curtis-demo-field-walk-20260728",
        "Demo · Field walkthrough",
        HERE / ".cache" / "mcp_video-289_singular_display-1785281112-170762099.mp4",
    ),
    (
        "Curtis-demo-drive-20260728",
        "Demo · Driver view",
        HERE / ".cache" / "IMG_3095-1785279932-141342585.mp4",
    ),
)


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


def import_demo(session_id: str, source_label: str, source: Path, started_at: float) -> None:
    if not source.exists():
        raise FileNotFoundError(f"missing converted demo video: {source}")
    session_dir = ARCHIVES / session_id
    recording = session_dir / "recording.mp4"
    metadata_path = session_dir / "metadata.json"
    if recording.exists() and metadata_path.exists():
        print(f"kept {session_id}: already imported")
        return
    duration = duration_seconds(source)
    session_dir.mkdir(parents=True, exist_ok=True)
    temporary = recording.with_name("recording.part.mp4")
    shutil.copyfile(source, temporary)
    temporary.replace(recording)
    metadata = {
        "session_id": session_id,
        "worker_name": "Curtis",
        "source": source_label,
        "started_at": started_at,
        "ended_at": started_at + duration,
        "status": "complete",
    }
    temporary_metadata = metadata_path.with_name("metadata.json.part")
    temporary_metadata.write_text(json.dumps(metadata, separators=(",", ":"), sort_keys=True))
    temporary_metadata.replace(metadata_path)
    (session_dir / "transcript.jsonl").touch(exist_ok=True)
    print(f"imported {session_id}: {duration:.1f}s H.264/AAC MP4")


def main() -> None:
    now = time.time()
    # Keep the field walk first in the archive, then the driver view directly
    # before it; exact wall-clock capture times were not available in the MOVs.
    durations = [duration_seconds(source) for _, _, source in DEMOS]
    cursor = now - sum(durations) - 5
    for (session_id, source_label, source), duration in zip(DEMOS, durations):
        import_demo(session_id, source_label, source, cursor)
        cursor += duration + 2


if __name__ == "__main__":
    main()
