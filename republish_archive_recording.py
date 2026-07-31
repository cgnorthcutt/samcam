#!/usr/bin/env python3
"""Safely replace one completed public archive MP4 without touching Live.

The normal publisher owns the Curtis live WebSocket.  This maintenance tool
uses a separate worker identity and sends only durable archived-recording
chunks, so it cannot blank or interrupt the live camera. It can replace the
primary improved MP4 or backfill the retained original MP4 used by Archive's
audio A/B comparison.

Example:

    SAMCAM_RELAY_URL=https://samcam.app \
      .venv/bin/python republish_archive_recording.py \
      Curtis-20260730T161648Z-b54a5006 \
      archives/Curtis-20260730T161648Z-b54a5006/recording.mastered.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp


RELAY_URL = os.environ.get("SAMCAM_RELAY_URL", "https://samcam-relay.onrender.com").rstrip("/")
MAINTENANCE_WORKER = (
    os.environ.get("SAMCAM_ARCHIVE_MAINTENANCE_WORKER", "SamCamArchiveMaintenance").strip()
    or "SamCamArchiveMaintenance"
)
ARCHIVE_RECORDING_MAGIC = b"SCAR"
ARCHIVE_ORIGINAL_RECORDING_MAGIC = b"SCOR"
ARCHIVE_RECORDING_CHUNK_BYTES = 1_500_000
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")


@dataclass(frozen=True)
class RecordingChunk:
    session_id: str
    index: int
    count: int
    size_bytes: int
    data: bytes
    variant: str = "improved"


def relay_websocket_url(worker: str, relay_url: str = RELAY_URL) -> str:
    parsed = urlsplit(relay_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SAMCAM_RELAY_URL must start with http:// or https://")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/ws/worker/{quote(worker)}", "", ""))


def validate_session_id(value: str) -> str:
    session_id = value.strip()
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError(f"invalid archive session ID: {value!r}")
    return session_id


def recording_chunks(
    session_id: str,
    path: Path,
    *,
    variant: str = "improved",
) -> list[RecordingChunk]:
    if variant not in {"improved", "original"}:
        raise ValueError("recording variant must be improved or original")
    source = path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"recording is missing: {source}")
    size_bytes = source.stat().st_size
    if size_bytes <= 0:
        raise ValueError("recording is empty")
    count = max(1, (size_bytes + ARCHIVE_RECORDING_CHUNK_BYTES - 1) // ARCHIVE_RECORDING_CHUNK_BYTES)
    chunks: list[RecordingChunk] = []
    with source.open("rb") as handle:
        for index in range(count):
            data = handle.read(ARCHIVE_RECORDING_CHUNK_BYTES)
            if not data:
                raise ValueError(f"recording ended before chunk {index}")
            chunks.append(RecordingChunk(session_id, index, count, size_bytes, data, variant))
    return chunks


def chunk_envelope(chunk: RecordingChunk) -> bytes:
    original = chunk.variant == "original"
    metadata = json.dumps(
        {
            "type": "archive_original_recording_chunk" if original else "archive_recording_chunk",
            "session_id": chunk.session_id,
            "index": chunk.index,
            "count": chunk.count,
            "size_bytes": chunk.size_bytes,
        },
        separators=(",", ":"),
    ).encode()
    magic = ARCHIVE_ORIGINAL_RECORDING_MAGIC if original else ARCHIVE_RECORDING_MAGIC
    return magic + len(metadata).to_bytes(4, "big") + metadata + chunk.data


async def republish(
    session_id: str,
    path: Path,
    worker: str,
    relay_url: str = RELAY_URL,
    *,
    variant: str = "improved",
) -> None:
    chunks = recording_chunks(session_id, path, variant=variant)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(relay_websocket_url(worker, relay_url), heartbeat=20, autoping=True) as websocket:
            for chunk in chunks:
                await websocket.send_bytes(chunk_envelope(chunk))
                print(f"Uploaded archive chunk {chunk.index + 1}/{chunk.count}")
            await websocket.send_json(
                {
                    "type": "archive_original_recording_complete" if variant == "original" else "archive_recording_complete",
                    "session_id": session_id,
                    "chunk_count": chunks[0].count,
                    "size_bytes": chunks[0].size_bytes,
                }
            )
            # WebSocket messages are ordered; this short yield lets the relay
            # commit the final completion message before the socket closes.
            await asyncio.sleep(0.2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", help="existing public archive session ID")
    parser.add_argument("recording", type=Path, help="validated mastered MP4 to upload")
    parser.add_argument("--variant", choices=("improved", "original"), default="improved", help="archive variant to upload (default: improved)")
    parser.add_argument("--worker", default=MAINTENANCE_WORKER, help="maintenance WebSocket identity")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the upload plan without connecting")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        session_id = validate_session_id(args.session_id)
        worker = args.worker.strip()
        if not WORKER_NAME.fullmatch(worker):
            raise ValueError(f"invalid maintenance worker: {args.worker!r}")
        chunks = recording_chunks(session_id, args.recording, variant=args.variant)
        if args.dry_run:
            print(
                f"Would upload the {args.variant} recording for {session_id} with {len(chunks)} chunk(s), "
                f"{chunks[0].size_bytes} bytes, via {relay_websocket_url(worker)}"
            )
            return 0
        asyncio.run(republish(session_id, args.recording, worker, variant=args.variant))
        print(f"Completed public archive {args.variant} upload: {session_id}")
    except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        print(f"Archive recording replacement failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
