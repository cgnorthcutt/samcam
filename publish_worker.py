#!/usr/bin/env python3
"""Publish a local Sam Cam stream to the public Sam Cam relay.

Run this beside ``server.py`` on the laptop that has the USB body camera.  It
opens an outbound connection only, so the laptop needs no public IP or router
configuration.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from contextlib import suppress
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

LOCAL_URL = os.environ.get("SAMCAM_LOCAL_URL", "http://127.0.0.1:8011").rstrip("/")
RELAY_URL = os.environ.get("SAMCAM_RELAY_URL", "https://samcam.app").rstrip("/")
WORKER = os.environ.get("SAMCAM_WORKER", "Curtis").strip() or "Curtis"
RECONNECT_SECONDS = 2.0


def relay_websocket_url() -> str:
    parsed = urlsplit(RELAY_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("SAMCAM_RELAY_URL must start with http:// or https://")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    prefix = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, f"{prefix}/ws/worker/{quote(WORKER)}", "", ""))


async def get_json(session: aiohttp.ClientSession, path: str) -> dict[str, Any] | None:
    try:
        async with session.get(f"{LOCAL_URL}{path}", timeout=aiohttp.ClientTimeout(total=2)) as response:
            if response.status != 200:
                return None
            payload = await response.json()
            return payload if isinstance(payload, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def publish_status(
    session: aiohttp.ClientSession,
    send: Any,
    stop: asyncio.Event,
) -> None:
    last_transcript_id = -1
    while not stop.is_set():
        stream = await get_json(session, "/api/stream")
        fresh_at = stream.get("last_live_frame_at") if stream else None
        try:
            live = bool(stream and stream.get("live") and fresh_at and time.time() - float(fresh_at) < 5)
        except (TypeError, ValueError):
            live = False
        await send({"type": "status", "live": live, "source": stream.get("source") if stream else None})

        transcript = await get_json(session, "/api/transcript")
        for line in (transcript or {}).get("lines", []):
            if not isinstance(line, dict):
                continue
            try:
                line_id = int(line.get("id", -1))
            except (TypeError, ValueError):
                continue
            if line_id > last_transcript_id:
                await send({"type": "transcript", "line": line})
                last_transcript_id = line_id
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def publish_frames(
    session: aiohttp.ClientSession,
    send: Any,
    stop: asyncio.Event,
) -> None:
    """Extract JPEG SOI/EOI ranges from local multipart MJPEG output."""
    while not stop.is_set():
        try:
            async with session.get(
                f"{LOCAL_URL}/stream.mjpg", timeout=aiohttp.ClientTimeout(total=None, sock_read=15)
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"local stream returned HTTP {response.status}")
                buffer = bytearray()
                async for chunk in response.content.iter_any():
                    if stop.is_set():
                        return
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        if start < 0:
                            if len(buffer) > 1_000_000:
                                del buffer[:-2]
                            break
                        if start:
                            del buffer[:start]
                        end = buffer.find(b"\xff\xd9", 2)
                        if end < 0:
                            break
                        frame = bytes(buffer[:end + 2])
                        del buffer[:end + 2]
                        if len(frame) <= 5_000_000:
                            await send(frame)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            print(f"  local stream: {exc}", file=sys.stderr)
            try:
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
            except asyncio.TimeoutError:
                pass


async def connected_publisher(stop: asyncio.Event) -> None:
    websocket_url = relay_websocket_url()
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(websocket_url, heartbeat=20, autoping=True) as websocket:
                    print(f"  publishing {WORKER} to {RELAY_URL}")
                    send_lock = asyncio.Lock()

                    async def send(payload: dict[str, Any] | bytes) -> None:
                        async with send_lock:
                            if isinstance(payload, bytes):
                                await websocket.send_bytes(payload)
                            else:
                                await websocket.send_json(payload)

                    await send({"type": "status", "live": False, "source": None})
                    status_task = asyncio.create_task(publish_status(session, send, stop))
                    frames_task = asyncio.create_task(publish_frames(session, send, stop))
                    stop_task = asyncio.create_task(stop.wait())
                    done, pending = await asyncio.wait(
                        {status_task, frames_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        with suppress(asyncio.CancelledError):
                            await task
                    for task in done:
                        if task is not stop_task:
                            with suppress(asyncio.CancelledError):
                                task.result()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                print(f"  relay connection: {exc}; retrying", file=sys.stderr)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
                except asyncio.TimeoutError:
                    pass


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    print(f"Sam Cam public publisher\n  local: {LOCAL_URL}\n  relay: {RELAY_URL}\n  worker: {WORKER}")
    await connected_publisher(stop)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
