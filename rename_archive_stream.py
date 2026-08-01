#!/usr/bin/env python3
"""Append a display title to one completed archive stream safely.

The public archive derives the visible title from a session's ``source``
metadata after an em dash. This command preserves the capture source, writes
the same metadata to the local archive cache, and sends a normal archive
metadata update to the relay. It does not touch the media, transcript, or
analytics objects.

Example:

    EGOCAPTURE_RELAY_URL=https://<your-relay-host> \
      python3 rename_archive_stream.py archives SESSION_ID "recording demo"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp


RELAY_URL = os.environ.get("EGOCAPTURE_RELAY_URL", "").rstrip("/")
WORKER = os.environ.get("EGOCAPTURE_ARCHIVE_MAINTENANCE_WORKER", "EgoCaptureArchiveMaintenance").strip() or "EgoCaptureArchiveMaintenance"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")


def relay_websocket_url(worker: str, relay_url: str = RELAY_URL) -> str:
    parsed = urlsplit(relay_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("EGOCAPTURE_RELAY_URL must start with http:// or https://")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/ws/worker/{quote(worker)}", "", ""))


def validated_title(value: str) -> str:
    title = " ".join(value.split())
    if not title or len(title) > 120:
        raise ValueError("title must contain 1 to 120 non-whitespace characters")
    return title


def renamed_metadata(metadata: dict[str, object], title: str) -> dict[str, object]:
    """Return metadata with exactly one UI title suffix and the original source."""
    session_id = str(metadata.get("session_id") or "").strip()
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError("metadata has an invalid session ID")
    source = str(metadata.get("source") or "camera capture")
    source = source.split(" — ", 1)[0].strip() or "camera capture"
    result = dict(metadata)
    result["source"] = f"{source} — {validated_title(title)}"
    return result


def metadata_path(archive_root: Path, session_id: str) -> Path:
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session ID")
    return archive_root.expanduser().resolve() / session_id / "metadata.json"


def read_metadata(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read metadata: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("metadata must be a JSON object")
    return payload


def write_metadata(path: Path, metadata: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(metadata, separators=(",", ":"), sort_keys=True))
    temporary.replace(path)


async def publish_metadata(metadata: dict[str, object], worker: str, relay_url: str = RELAY_URL) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(relay_websocket_url(worker, relay_url), heartbeat=20, autoping=True) as websocket:
            await websocket.send_json({"type": "archive_session", "session": metadata})
            # WebSocket messages are ordered; retain the socket briefly so the
            # relay consumes the durable metadata message before we close.
            await asyncio.sleep(0.2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("session_id")
    parser.add_argument("title", help="suffix shown after the stream timestamp")
    parser.add_argument("--worker", default=WORKER)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    worker = args.worker.strip()
    try:
        if not WORKER_NAME.fullmatch(worker):
            raise ValueError("invalid worker name")
        title = validated_title(args.title)
        path = metadata_path(args.archive_root, args.session_id.strip())
        original = read_metadata(path)
        updated = renamed_metadata(original, title)
        if args.dry_run:
            print(json.dumps(updated, sort_keys=True))
            return 0
        # Publish first: a local cache must never advertise a title which did
        # not make it to the durable archive.
        asyncio.run(publish_metadata(updated, worker))
        write_metadata(path, updated)
        print(f"Renamed {updated['session_id']} to {title!r}")
    except (ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        print(f"Archive rename failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
