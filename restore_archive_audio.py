#!/usr/bin/env python3
"""Create a clean, speech-first MP4 from an archived Sam Cam recording.

This is intentionally an *offline* utility.  It never opens a capture device,
does not affect the live relay, and leaves the source recording untouched.
The output is written to a unique temporary file, probed, and atomically moved
into place only after it is known to contain the original video stream plus a
new AAC audio stream.  It is therefore safe to re-run after a crash or laptop
sleep.

The default profile is conservative for close-mic speech:

* SoX-quality resampling to 48 kHz (when the installed FFmpeg supports it)
* high/low pass filtering to remove rumble and ultrasonic/screechy content
* de-clicking/de-clipping and FFT denoising when those filters are available
* two-pass EBU R128 loudness normalization and a final true-peak limiter
* AAC-LC remux while copying every video stream without re-encoding it

The optional --mains-hz setting adds narrow hum notches.  Leave it disabled
unless a steady 50 Hz or 60 Hz electrical hum is audible: unnecessary notches
can make some voices sound thinner.

Examples:

    # Creates archives/.../recording.restored.mp4 next to the source.
    python3 restore_archive_audio.py archives/Curtis-.../recording.mp4

    # Inspect the exact deterministic plan without writing media.
    python3 restore_archive_audio.py recording.mp4 --dry-run --mains-hz 60

    # Replace an earlier restored output only after the new one validates.
    python3 restore_archive_audio.py recording.mp4 --overwrite
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


TARGET_SAMPLE_RATE = 48_000
TARGET_CHANNELS = 1
TARGET_AUDIO_BITRATE = "96k"
LOUDNESS_TARGET_LUFS = -16.0
LOUDNESS_RANGE_LU = 11.0
TRUE_PEAK_DB = -2.0
LIMITER_PEAK = 0.89

REQUIRED_FILTERS = frozenset({"aresample", "highpass", "lowpass", "loudnorm", "alimiter"})
OPTIONAL_REPAIR_FILTERS = frozenset({"adeclick", "adeclip", "afftdn"})
LOUDNORM_FIELDS = (
    "input_i",
    "input_lra",
    "input_tp",
    "input_thresh",
    "target_offset",
)
VIDEO_IDENTITY_FIELDS = (
    "codec_name",
    "codec_tag_string",
    "width",
    "height",
    "pix_fmt",
)


class RestorationError(RuntimeError):
    """The source was not modified and no validated output was produced."""


@dataclass(frozen=True)
class AudioRestorationPlan:
    """All deterministic choices used for one restoration run."""

    source: Path
    destination: Path
    first_pass_filter: str
    second_pass_prefix: str
    enabled_optional_filters: tuple[str, ...]
    mains_hz: int


def command_path(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise RestorationError(f"{binary} is required (try: brew install ffmpeg)")
    return path


def parse_available_filters(output: str) -> set[str]:
    """Extract names from ``ffmpeg -filters`` across FFmpeg versions."""
    filters: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^\s*[A-Z.]+\s+([A-Za-z0-9_]+)\s+", line)
        if match:
            filters.add(match.group(1))
    return filters


def installed_filters(ffmpeg: str) -> set[str]:
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-500:]
        raise RestorationError(detail or "FFmpeg could not list its audio filters")
    return parse_available_filters(completed.stdout)


def default_destination(source: Path) -> Path:
    return source.with_name(f"{source.stem}.restored{source.suffix}")


def restoration_filters(
    available: Iterable[str],
    *,
    mains_hz: int = 0,
    repair_clicks: bool = True,
    denoise: bool = True,
) -> tuple[str, tuple[str, ...]]:
    """Return a conservative, deterministic filter chain.

    Optional restoration filters are selected only when compiled into the
    installed FFmpeg.  The basic speech pass remains usable on minimal FFmpeg
    builds, but required timing/loudness/limiter filters must be present.
    """
    available_set = set(available)
    missing = sorted(REQUIRED_FILTERS - available_set)
    if missing:
        raise RestorationError(
            "FFmpeg lacks required archive-audio filters: " + ", ".join(missing)
        )
    if mains_hz not in (0, 50, 60):
        raise RestorationError("--mains-hz must be 0, 50, or 60")

    # A 75 Hz high-pass safely removes DC/handling rumble and the fundamental
    # of mains hum without aggressively cutting normal speech.  7.2 kHz keeps
    # useful consonant detail from the camera's 16 kHz capture while removing
    # the high-frequency feedback/screech that listeners reported.
    chain = [
        "aresample=48000:resampler=soxr:precision=28:cheby=1",
        "highpass=f=75:p=2",
        "lowpass=f=7200:p=2",
    ]
    enabled: list[str] = []

    # Mains notches are deliberately opt-in.  The base high-pass already
    # removes the strongest fundamental.  Narrow harmonic notches help a
    # known electrical hum but can otherwise color a low voice.
    if mains_hz:
        for harmonic, reduction in ((2, -12), (3, -8)):
            chain.append(
                f"equalizer=f={mains_hz * harmonic}:t=q:w=1:g={reduction}"
            )
        enabled.append(f"{mains_hz}Hz-hum-notches")

    if repair_clicks and "adeclick" in available_set:
        chain.append("adeclick=w=55:o=75:a=2:t=2")
        enabled.append("adeclick")
    if repair_clicks and "adeclip" in available_set:
        # Only repairs obviously clipped peaks; it is not used as a blanket
        # distortion effect.
        chain.append("adeclip=w=55:o=75:a=8:t=10")
        enabled.append("adeclip")
    if denoise and "afftdn" in available_set:
        # Moderate adaptive reduction avoids the metallic artifacts that much
        # stronger FFT denoisers can introduce in a close-mic spoken voice.
        chain.append("afftdn=nr=8:nf=-42:tn=1:gs=6")
        enabled.append("afftdn")

    return ",".join(chain), tuple(enabled)


def loudnorm_measure_filter(prefix: str) -> str:
    return (
        f"{prefix},loudnorm=I={LOUDNESS_TARGET_LUFS}:LRA={LOUDNESS_RANGE_LU}:"
        f"TP={TRUE_PEAK_DB}:dual_mono=true:print_format=json"
    )


def parse_loudnorm_measurement(stderr: str) -> dict[str, float]:
    """Read the final JSON block emitted by FFmpeg's loudnorm filter."""
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"\{\s*\"input_i\"", stderr):
        try:
            value, _ = decoder.raw_decode(stderr[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        raise RestorationError("FFmpeg did not return loudness measurements")

    measurement = candidates[-1]
    parsed: dict[str, float] = {}
    for field in LOUDNORM_FIELDS:
        try:
            number = float(measurement[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RestorationError(f"invalid loudness measurement: {field}") from exc
        if not math.isfinite(number):
            raise RestorationError(f"non-finite loudness measurement: {field}")
        parsed[field] = number
    return parsed


def loudnorm_apply_filter(prefix: str, measured: dict[str, float]) -> str:
    """Build the second loudnorm pass from measured first-pass values."""
    values = ":".join(
        (
            f"measured_I={measured['input_i']:.6f}",
            f"measured_LRA={measured['input_lra']:.6f}",
            f"measured_TP={measured['input_tp']:.6f}",
            f"measured_thresh={measured['input_thresh']:.6f}",
            f"offset={measured['target_offset']:.6f}",
        )
    )
    return (
        f"{prefix},loudnorm=I={LOUDNESS_TARGET_LUFS}:LRA={LOUDNESS_RANGE_LU}:"
        f"TP={TRUE_PEAK_DB}:{values}:linear=true:dual_mono=true:print_format=summary,"
        f"alimiter=limit={LIMITER_PEAK}:level=0:attack=5:release=50:latency=1"
    )


def quiet_input_filter(prefix: str) -> str:
    """Keep an all-silent but otherwise valid recording playable.

    FFmpeg reports ``-inf`` loudness for an entirely quiet camera interval,
    which cannot be used as a two-pass R128 measurement.  It is not an audio
    restoration failure: preserve the prepared AAC audio, avoid artificial
    gain, and retain the same safety limiter.
    """
    return f"{prefix},alimiter=limit={LIMITER_PEAK}:level=0:attack=5:release=50:latency=1"


def measure_command(ffmpeg: str, source: Path, filter_chain: str) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        loudnorm_measure_filter(filter_chain),
        "-f",
        "null",
        "-",
    ]


def render_command(ffmpeg: str, source: Path, destination: Path, filter_chain: str) -> list[str]:
    """Build the remux: video/subtitles are copied; only audio is encoded."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v?",
        "-map",
        "0:a:0",
        "-map",
        "0:s?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c:v",
        "copy",
        "-c:s",
        "copy",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-b:a",
        TARGET_AUDIO_BITRATE,
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-ac",
        str(TARGET_CHANNELS),
        "-af",
        filter_chain,
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-movflags",
        "+faststart",
        str(destination),
    ]


def run_checked(command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RestorationError(f"FFmpeg timed out after {timeout:.0f}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-1_000:]
        raise RestorationError(detail or f"FFmpeg exited with status {completed.returncode}")
    return completed


def probe_media(ffprobe: str, path: Path) -> dict[str, Any]:
    completed = run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,codec_tag_string,width,height,pix_fmt,avg_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RestorationError("ffprobe returned invalid JSON") from exc


def stream_list(probe: dict[str, Any], stream_type: str) -> list[dict[str, Any]]:
    return [
        stream for stream in probe.get("streams", [])
        if isinstance(stream, dict) and stream.get("codec_type") == stream_type
    ]


def duration_seconds(probe: dict[str, Any]) -> float:
    try:
        value = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RestorationError("recording has no measurable duration") from exc
    if not math.isfinite(value) or value <= 0:
        raise RestorationError("recording has no positive duration")
    return value


def validate_restored_media(source_probe: dict[str, Any], restored_probe: dict[str, Any]) -> None:
    """Ensure a failed audio job can never replace a valid source-equivalent MP4."""
    source_video = stream_list(source_probe, "video")
    restored_video = stream_list(restored_probe, "video")
    if not source_video or len(source_video) != len(restored_video):
        raise RestorationError("restored output does not retain every video stream")
    for original, restored in zip(source_video, restored_video):
        identity_original = tuple(original.get(field) for field in VIDEO_IDENTITY_FIELDS)
        identity_restored = tuple(restored.get(field) for field in VIDEO_IDENTITY_FIELDS)
        if identity_original != identity_restored:
            raise RestorationError("restored output changed a copied video stream")

    audio = stream_list(restored_probe, "audio")
    if len(audio) != 1 or audio[0].get("codec_name") != "aac":
        raise RestorationError("restored output does not contain one AAC audio stream")
    if str(audio[0].get("sample_rate")) != str(TARGET_SAMPLE_RATE):
        raise RestorationError("restored audio has an unexpected sample rate")
    if int(audio[0].get("channels") or 0) != TARGET_CHANNELS:
        raise RestorationError("restored audio has an unexpected channel count")

    source_duration = duration_seconds(source_probe)
    restored_duration = duration_seconds(restored_probe)
    if abs(source_duration - restored_duration) > 0.35:
        raise RestorationError("restored output duration differs from the source")


def acquire_lock(destination: Path) -> Path:
    lock = destination.with_name(f".{destination.name}.restore.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RestorationError(f"another restoration is already running: {lock.name}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\n")
    return lock


def plan_for(
    source: Path,
    destination: Path,
    *,
    available_filters: Iterable[str],
    mains_hz: int,
    repair_clicks: bool,
    denoise: bool,
) -> AudioRestorationPlan:
    prefix, enabled = restoration_filters(
        available_filters,
        mains_hz=mains_hz,
        repair_clicks=repair_clicks,
        denoise=denoise,
    )
    return AudioRestorationPlan(
        source=source,
        destination=destination,
        first_pass_filter=loudnorm_measure_filter(prefix),
        second_pass_prefix=prefix,
        enabled_optional_filters=enabled,
        mains_hz=mains_hz,
    )


def restore(
    source: Path,
    destination: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    available_filters: Iterable[str],
    mains_hz: int = 0,
    repair_clicks: bool = True,
    denoise: bool = True,
    overwrite: bool = False,
) -> str:
    """Restore ``source`` to ``destination`` and return its final state.

    Returns ``"created"`` or ``"already-restored"``.  In both cases the
    destination has passed the same video/audio validation.
    """
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise RestorationError(f"source recording is missing or empty: {source}")
    if source == destination:
        raise RestorationError("destination must be different from source")

    source_probe = probe_media(ffprobe, source)
    if not stream_list(source_probe, "video"):
        raise RestorationError("source recording has no video stream")
    if not stream_list(source_probe, "audio"):
        raise RestorationError("source recording has no audio stream to restore")
    duration_seconds(source_probe)

    plan = plan_for(
        source,
        destination,
        available_filters=available_filters,
        mains_hz=mains_hz,
        repair_clicks=repair_clicks,
        denoise=denoise,
    )
    if destination.exists() and not overwrite:
        restored_probe = probe_media(ffprobe, destination)
        validate_restored_media(source_probe, restored_probe)
        return "already-restored"

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(destination)
    temporary = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.partial{destination.suffix}"
    )
    try:
        # A destination may have appeared between the preflight and lock
        # creation; never overwrite it unless explicitly requested.
        if destination.exists() and not overwrite:
            restored_probe = probe_media(ffprobe, destination)
            validate_restored_media(source_probe, restored_probe)
            return "already-restored"

        measured_run = run_checked(
            measure_command(ffmpeg, source, plan.second_pass_prefix), timeout=900
        )
        try:
            measured = parse_loudnorm_measurement(measured_run.stderr)
            apply_filter = loudnorm_apply_filter(plan.second_pass_prefix, measured)
        except RestorationError as exc:
            if not str(exc).startswith("non-finite loudness measurement"):
                raise
            # A silent interval has no usable LUFS target. Preserve it as
            # silence rather than falling back to an unmastered 16 kHz file.
            apply_filter = quiet_input_filter(plan.second_pass_prefix)
        run_checked(render_command(ffmpeg, source, temporary, apply_filter), timeout=1_800)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RestorationError("FFmpeg did not create an output recording")
        restored_probe = probe_media(ffprobe, temporary)
        validate_restored_media(source_probe, restored_probe)
        os.replace(temporary, destination)
        return "created"
    finally:
        temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("source", type=Path, help="source MP4 whose video must remain unchanged")
    argument_parser.add_argument("-o", "--output", type=Path, help="restored MP4 path (default: SOURCE.restored.mp4)")
    argument_parser.add_argument("--mains-hz", type=int, choices=(0, 50, 60), default=0, help="add optional 50/60 Hz hum harmonic notches")
    argument_parser.add_argument("--no-decrackle", action="store_true", help="disable adeclick/adeclip even when FFmpeg provides them")
    argument_parser.add_argument("--no-denoise", action="store_true", help="disable adaptive FFT denoising")
    argument_parser.add_argument("--overwrite", action="store_true", help="atomically replace an existing restored output after validation")
    argument_parser.add_argument("--dry-run", action="store_true", help="print the selected pipeline and commands without processing media")
    return argument_parser


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    destination = (args.output or default_destination(source)).expanduser().resolve()
    try:
        ffmpeg = command_path("ffmpeg")
        ffprobe = command_path("ffprobe")
        available = installed_filters(ffmpeg)
        plan = plan_for(
            source,
            destination,
            available_filters=available,
            mains_hz=args.mains_hz,
            repair_clicks=not args.no_decrackle,
            denoise=not args.no_denoise,
        )
        if args.dry_run:
            print(json.dumps({
                "source": str(source),
                "output": str(destination),
                "mains_hz": plan.mains_hz,
                "optional_repairs": list(plan.enabled_optional_filters),
                "measure_command": measure_command(ffmpeg, source, plan.second_pass_prefix),
                "render_command_template": render_command(
                    ffmpeg,
                    source,
                    destination,
                    loudnorm_apply_filter(plan.second_pass_prefix, {
                        "input_i": -24.0,
                        "input_lra": 7.0,
                        "input_tp": -6.0,
                        "input_thresh": -34.0,
                        "target_offset": 0.0,
                    }),
                ),
            }, indent=2))
            return 0

        state = restore(
            source,
            destination,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            available_filters=available,
            mains_hz=args.mains_hz,
            repair_clicks=not args.no_decrackle,
            denoise=not args.no_denoise,
            overwrite=args.overwrite,
        )
        print(json.dumps({
            "status": state,
            "source": str(source),
            "output": str(destination),
            "profile": "speech-safe-offline-v1",
            "optional_repairs": list(plan.enabled_optional_filters),
        }))
        return 0
    except RestorationError as exc:
        print(f"audio restoration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
