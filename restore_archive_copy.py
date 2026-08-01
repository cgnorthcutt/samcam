#!/usr/bin/env python3
"""Restore a mistakenly deleted archive under a fresh durable ID.

Deleted sessions are intentionally tombstoned by the relay so a reconnecting
publisher cannot resurrect a recording the user meant to remove. When a
deletion itself was accidental, this helper restores the local MP4 under an
explicitly supplied new ID while retaining the original capture timestamp and
display title. It never modifies or deletes the source archive folder.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rename_archive_stream import (
    RELAY_URL,
    SESSION_ID,
    WORKER,
    WORKER_NAME,
    publish_metadata,
    read_metadata,
    renamed_metadata,
    validated_title,
)
from republish_archive_recording import republish


def restored_metadata(source: dict[str, object], restored_id: str, title: str) -> dict[str, object]:
    """Create a new archive identity without changing the original timestamp."""
    if not SESSION_ID.fullmatch(restored_id):
        raise ValueError("restored session ID is invalid")
    payload = dict(source)
    payload["session_id"] = restored_id
    return renamed_metadata(payload, title)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_directory", type=Path, help="local archive session directory")
    parser.add_argument("restored_session_id", help="new archive ID, distinct from the deleted ID")
    parser.add_argument("title", help="display-title suffix")
    parser.add_argument("--worker", default=WORKER)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        worker = args.worker.strip()
        if not WORKER_NAME.fullmatch(worker):
            raise ValueError("invalid worker name")
        source_directory = args.source_directory.expanduser().resolve()
        source = read_metadata(source_directory / "metadata.json")
        metadata = restored_metadata(source, args.restored_session_id.strip(), validated_title(args.title))
        recording = source_directory / "recording.mp4"
        if not recording.is_file() or recording.stat().st_size <= 0:
            raise ValueError("source archive does not contain a playable recording.mp4")
        if args.dry_run:
            print(f"Would restore {source['session_id']} as {metadata['session_id']}: {metadata['source']}")
            return 0
        asyncio.run(publish_metadata(metadata, worker, RELAY_URL))
        asyncio.run(republish(str(metadata["session_id"]), recording, worker, RELAY_URL, variant="improved"))
        print(f"Restored {source['session_id']} as {metadata['session_id']}")
    except (OSError, ValueError) as exc:
        print(f"Archive restoration failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
