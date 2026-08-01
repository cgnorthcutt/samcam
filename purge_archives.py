#!/usr/bin/env python3
"""Remove explicitly named archived sessions from a configured relay.

This utility only asks the Render relay to remove already-uploaded archive
records.  It never reads, modifies, or deletes the local ``archives/`` folder.

Examples:
    .venv/bin/python purge_archives.py camera-lab-20260730T052210Z-5f4ba65e
    EGOCAPTURE_ARCHIVE_MAINTENANCE_WORKER="Archive cleanup" \
      .venv/bin/python purge_archives.py --dry-run SESSION_ID
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp


# Keep the same relay and worker configuration conventions as publish_worker.py.
RELAY_URL = os.environ.get("EGOCAPTURE_RELAY_URL", "").rstrip("/")
# Archive deletion does not need to impersonate an active camera worker.  A
# distinct default avoids replacing that worker's live WebSocket state.
WORKER = os.environ.get("EGOCAPTURE_ARCHIVE_MAINTENANCE_WORKER", "EgoCaptureArchiveMaintenance").strip() or "EgoCaptureArchiveMaintenance"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")


def relay_websocket_url(worker: str) -> str:
    """Return the worker WebSocket endpoint for the configured Render relay."""
    parsed = urlsplit(RELAY_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("EGOCAPTURE_RELAY_URL must start with http:// or https://")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/ws/worker/{quote(worker)}", "", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete explicitly named uploaded archive sessions from the configured relay."
    )
    parser.add_argument(
        "session_ids",
        nargs="+",
        metavar="SESSION_ID",
        help="one or more archive session IDs to remove from the public relay",
    )
    parser.add_argument(
        "--worker",
        default=WORKER,
        help="maintenance WebSocket name (default: EGOCAPTURE_ARCHIVE_MAINTENANCE_WORKER)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print requests without opening a WebSocket",
    )
    return parser.parse_args()


def validated_ids(raw_ids: list[str]) -> list[str]:
    """Validate relay-compatible identifiers and avoid duplicate delete requests."""
    session_ids: list[str] = []
    for raw_session_id in raw_ids:
        session_id = raw_session_id.strip()
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError(f"invalid archive session ID: {raw_session_id!r}")
        if session_id not in session_ids:
            session_ids.append(session_id)
    return session_ids


async def purge(session_ids: list[str], worker: str) -> None:
    websocket_url = relay_websocket_url(worker)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(websocket_url, heartbeat=20, autoping=True) as websocket:
            for session_id in session_ids:
                await websocket.send_json({"type": "archive_delete", "session_id": session_id})
                print(f"Sent public archive delete request: {session_id}")

            # The relay processes text messages in order.  A short yield gives it
            # a chance to consume the final message before this utility closes.
            await asyncio.sleep(0.1)


def main() -> int:
    args = parse_args()
    worker = args.worker.strip()
    if not WORKER_NAME.fullmatch(worker):
        print(f"Invalid worker name: {args.worker!r}", file=sys.stderr)
        return 2
    try:
        session_ids = validated_ids(args.session_ids)
        if args.dry_run:
            for session_id in session_ids:
                print(f"Would send: {{'type': 'archive_delete', 'session_id': '{session_id}'}}")
            return 0
        asyncio.run(purge(session_ids, worker))
    except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        print(f"Archive purge failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
