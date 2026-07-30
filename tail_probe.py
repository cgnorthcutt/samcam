#!/usr/bin/env python3
"""
Answer the one question chunked streaming depends on: while the camera is
plugged in, does the Mac ever see new footage appear?

Run this, then press record on the camera and let it run for a minute or two.

Three outcomes, and they decide what kind of streaming is possible:

  GROWING   an existing file's size keeps increasing and we can read the new
            bytes -> real chunked streaming, a few seconds of lag.

  NEW FILES files only appear complete, one per loop segment -> streaming still
            works, but lag is one segment (whatever the camera's loop length is,
            usually 1-5 minutes).

  NOTHING   the volume never changes while attached. Either the camera stops
            recording in USB disk mode, or macOS is serving us a cached view of
            the FAT table and never re-reads it. Then nothing can stream over
            USB, at any lag.

The last case has a workaround worth testing (--remount), which forces macOS to
drop its cached view of the volume between polls.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

F_NOCACHE = 48  # macOS: bypass the unified buffer cache for this fd

VIDEO_EXTS = {".avi", ".mov", ".mp4"}
POLL = 2.0


def find_video_dir() -> Path | None:
    for volume in sorted(Path("/Volumes").glob("*")):
        for name in ("VIDEO", "video", "DCIM"):
            candidate = volume / name
            try:
                if candidate.is_dir() and any(
                    p.suffix.lower() in VIDEO_EXTS for p in candidate.iterdir()
                ):
                    return candidate
            except OSError:
                continue
    return None


def uncached_size(path: Path) -> int:
    """Read the file end-to-end with caching disabled to get a true size.

    stat() alone can report a stale size: macOS assumes it owns the filesystem
    and caches FAT metadata, while the camera is writing to the flash behind its
    back. This at least defeats the page cache for file contents.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return -1
    try:
        fcntl.fcntl(fd, F_NOCACHE, 1)
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def remount(volume: Path) -> bool:
    """Force macOS to forget everything it thinks it knows about the volume."""
    for action in ("unmount", "mount"):
        result = subprocess.run(
            ["diskutil", action, str(volume) if action == "unmount" else device_of(volume)],
            capture_output=True,
        )
        if result.returncode != 0:
            return False
        time.sleep(1.0)
    return True


def device_of(volume: Path) -> str:
    out = subprocess.run(["diskutil", "info", str(volume)], capture_output=True).stdout.decode()
    for line in out.splitlines():
        if "Device Node" in line:
            return line.split(":")[-1].strip()
    return str(volume)


def snapshot(video_dir: Path) -> dict[str, int]:
    try:
        return {
            p.name: p.stat().st_size
            for p in video_dir.iterdir()
            if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")
        }
    except OSError:
        return {}


def main() -> None:
    use_remount = "--remount" in sys.argv

    video_dir = find_video_dir()
    if video_dir is None:
        sys.exit("no camera volume found -- plug it in and wait for it to mount")
    volume = video_dir.parent

    print(f"\nwatching {video_dir}")
    print(f"remount between polls: {'yes' if use_remount else 'no'}")
    print("\n>>> press RECORD on the camera now, and let it run a minute <<<\n")

    baseline = snapshot(video_dir)
    for name, size in sorted(baseline.items()):
        print(f"   {name:16} {size/1e6:8.2f} MB")
    print(f"\n   {len(baseline)} clips at start. watching for changes...\n")

    grew, appeared, start = set(), set(), time.time()
    previous = dict(baseline)

    try:
        while True:
            time.sleep(POLL)
            if use_remount and not remount(volume):
                print("   remount failed -- is the volume busy?")
            current = snapshot(video_dir)

            for name, size in sorted(current.items()):
                was = previous.get(name)
                if was is None:
                    real = uncached_size(video_dir / name)
                    print(f"[{time.strftime('%H:%M:%S')}] NEW    {name}  {size/1e6:.2f} MB "
                          f"(uncached read: {real/1e6:.2f} MB)")
                    appeared.add(name)
                elif size != was:
                    real = uncached_size(video_dir / name)
                    print(f"[{time.strftime('%H:%M:%S')}] GREW   {name}  "
                          f"{was/1e6:.2f} -> {size/1e6:.2f} MB "
                          f"(uncached read: {real/1e6:.2f} MB)")
                    grew.add(name)

            for name in sorted(set(previous) - set(current)):
                print(f"[{time.strftime('%H:%M:%S')}] GONE   {name}")

            previous = current
    except KeyboardInterrupt:
        elapsed = time.time() - start
        print(f"\n\n   watched for {elapsed:.0f}s")
        print(f"   files that grew in place : {len(grew)}  {sorted(grew) or ''}")
        print(f"   files that newly appeared: {len(appeared)}  {sorted(appeared) or ''}")
        print()
        if grew:
            print("   => GROWING. Real chunked streaming is possible, a few seconds of lag.")
        elif appeared:
            print("   => NEW FILES only. Streaming works at one-segment lag.")
        elif use_remount:
            print("   => NOTHING, even with remounts. The camera is not recording while")
            print("      attached over USB. No streaming is possible on this cable.")
        else:
            print("   => NOTHING changed. Before concluding, try again with:")
            print("         ./tail_probe.py --remount")
            print("      which forces macOS to re-read the volume instead of trusting")
            print("      its cached copy of the FAT table.")
        print()


if __name__ == "__main__":
    main()
