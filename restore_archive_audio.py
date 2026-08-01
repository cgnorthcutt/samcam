#!/usr/bin/env python3
"""Create a clean, speech-first MP4 from an archived Ego Capture recording.

This is intentionally an *offline* utility.  It never opens a capture device,
does not affect the live relay, and leaves the source recording untouched.
The output is written to a unique temporary file, probed, and atomically moved
into place only after it is known to contain the original video stream plus a
new AAC audio stream.  It is therefore safe to re-run after a crash or laptop
sleep.

The default profile is conservative for degraded close-mic speech. Healthy
full-bandwidth stereo audio bypasses this profile and is remuxed packet for
packet instead:

* SoX-quality resampling to 48 kHz (when the installed FFmpeg supports it)
* high/low pass filtering to remove rumble and ultrasonic/screechy content
* a gentle speech band-pass and loudness pass by default
* optional de-clicking/de-clipping and FFT denoising only when explicitly
  requested for a recording that actually has those defects
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
from typing import Any, Iterable, Literal, Sequence


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
AUDIO_IDENTITY_FIELDS = (
    "codec_name",
    "profile",
    "sample_rate",
    "channels",
    "channel_layout",
)

# Do not subject already healthy stereo source audio to speech repair.  This
# intentionally recognizes high-quality capture conservatively: a stereo,
# full-bandwidth signal with enough encoded bandwidth to preserve ambience and
# spatial cues.  A 16 kHz mono body-camera microphone will not match this
# profile and therefore still receives the restoration path below.
PRESERVE_MIN_SAMPLE_RATE = 44_100
PRESERVE_MIN_CHANNELS = 2
PRESERVE_MIN_BITRATE = 80_000


class RestorationError(RuntimeError):
    """The source was not modified and no validated output was produced."""


@dataclass(frozen=True)
class AudioMasteringDecision:
    """Whether archive audio needs repair or can be preserved exactly."""

    mode: Literal["preserve", "restore"]
    reason: str


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
    repair_clicks: bool = False,
    denoise: bool = False,
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

    # This is purposefully a *gentle* default.  The camera's 16 kHz mono AAC
    # audio has already discarded frequencies above 8 kHz; applying adaptive
    # denoisers or declippers blindly to it can create the metallic/crackly
    # artifacts they are meant to solve.  Removing sub-speech rumble and the
    # harsh upper band makes the track more intelligible without attempting to
    # invent fidelity that is not present in the original capture.
    chain = [
        "aresample=48000:resampler=soxr:precision=28:cheby=1",
        "highpass=f=100:p=2",
        "lowpass=f=6000:p=2",
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
        # This is intentionally opt-in: de-clipping can add audible harmonic
        # distortion to a low-bitrate speech track that was never clipped.
        chain.append("adeclip=w=55:o=75:a=8:t=10")
        enabled.append("adeclip")
    if denoise and "afftdn" in available_set:
        # Only use on a confirmed steady-noise recording.  Even modest
        # adaptive denoising can create warble on 16 kHz AAC speech.
        chain.append("afftdn=nr=4:nf=-42:tn=1:gs=4")
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


def preservation_command(ffmpeg: str, source: Path, destination: Path) -> list[str]:
    """Remux healthy capture audio without decoding or re-encoding it.

    ``-c:a copy`` keeps the AAC access units intact.  A later packet-hash
    verification makes that preservation contractual rather than merely an
    FFmpeg command-line intention.
    """
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
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-fflags",
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
            "format=duration:stream=index,codec_type,codec_name,codec_tag_string,profile,width,height,pix_fmt,avg_frame_rate,sample_rate,channels,channel_layout,bit_rate",
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


def audio_mastering_decision(source_probe: dict[str, Any]) -> AudioMasteringDecision:
    """Select lossless pass-through only for a healthy high-quality source.

    The policy is deliberately structural rather than branded-device-specific:
    any 44.1/48 kHz+ stereo source with sufficient bitrate is preserved.  This
    means the Meta Oakley Vanguard originals qualify without applying a
    potentially destructive mono speech-cleanup profile, while low-rate mono
    body-camera audio remains eligible for restoration.
    """
    audio_streams = stream_list(source_probe, "audio")
    if len(audio_streams) != 1:
        return AudioMasteringDecision("restore", "source does not contain exactly one audio stream")

    audio = audio_streams[0]
    try:
        sample_rate = int(audio.get("sample_rate") or 0)
        channels = int(audio.get("channels") or 0)
        bitrate = int(audio.get("bit_rate") or 0)
    except (TypeError, ValueError):
        return AudioMasteringDecision("restore", "source audio metadata is incomplete")

    if sample_rate < PRESERVE_MIN_SAMPLE_RATE:
        return AudioMasteringDecision("restore", f"sample rate {sample_rate} Hz is below preservation threshold")
    if channels < PRESERVE_MIN_CHANNELS:
        return AudioMasteringDecision("restore", f"{channels}-channel source is below preservation threshold")
    if bitrate < PRESERVE_MIN_BITRATE:
        return AudioMasteringDecision("restore", f"audio bitrate {bitrate} is below preservation threshold")
    return AudioMasteringDecision(
        "preserve",
        f"healthy {sample_rate} Hz, {channels}-channel, {bitrate} b/s source audio",
    )


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


def validate_preserved_media(source_probe: dict[str, Any], preserved_probe: dict[str, Any]) -> None:
    """Validate remuxed source audio without accepting an audio downgrade."""
    source_video = stream_list(source_probe, "video")
    preserved_video = stream_list(preserved_probe, "video")
    if not source_video or len(source_video) != len(preserved_video):
        raise RestorationError("preserved output does not retain every video stream")
    for original, preserved in zip(source_video, preserved_video):
        original_identity = tuple(original.get(field) for field in VIDEO_IDENTITY_FIELDS)
        preserved_identity = tuple(preserved.get(field) for field in VIDEO_IDENTITY_FIELDS)
        if original_identity != preserved_identity:
            raise RestorationError("preserved output changed a copied video stream")

    source_audio = stream_list(source_probe, "audio")
    preserved_audio = stream_list(preserved_probe, "audio")
    if len(source_audio) != 1 or len(preserved_audio) != 1:
        raise RestorationError("preserved output does not retain exactly one audio stream")
    original_identity = tuple(source_audio[0].get(field) for field in AUDIO_IDENTITY_FIELDS)
    preserved_identity = tuple(preserved_audio[0].get(field) for field in AUDIO_IDENTITY_FIELDS)
    if original_identity != preserved_identity:
        raise RestorationError("preserved output changed source audio metadata")

    source_duration = duration_seconds(source_probe)
    preserved_duration = duration_seconds(preserved_probe)
    if abs(source_duration - preserved_duration) > 0.35:
        raise RestorationError("preserved output duration differs from the source")


def encoded_audio_packet_hashes(ffprobe: str, path: Path) -> tuple[str, ...]:
    """Return SHA-256 values for the encoded packets in the first audio stream.

    FFprobe hashes each packet payload without decoding it.  Equality proves a
    stream-copy remux did not alter the original encoded audio data, unlike a
    comparison of decoded waveforms which could miss a lossy re-encode.
    """
    completed = run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=data_hash",
            "-show_data_hash",
            "sha256",
            "-of",
            "compact=p=0:nk=1",
            str(path),
        ],
        timeout=120,
    )
    hashes = tuple(
        line.split("SHA256:", 1)[1].strip()
        for line in completed.stdout.splitlines()
        if "SHA256:" in line
    )
    if not hashes:
        raise RestorationError("FFprobe did not find encoded audio packets to hash")
    return hashes


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
    repair_clicks: bool = False,
    denoise: bool = False,
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
    decision = audio_mastering_decision(source_probe)

    plan: AudioRestorationPlan | None = None
    source_packet_hashes: tuple[str, ...] | None = None
    if decision.mode == "preserve":
        source_packet_hashes = encoded_audio_packet_hashes(ffprobe, source)
    else:
        plan = plan_for(
            source,
            destination,
            available_filters=available_filters,
            mains_hz=mains_hz,
            repair_clicks=repair_clicks,
            denoise=denoise,
        )

    def validate_output(output_probe: dict[str, Any], output_path: Path) -> None:
        if decision.mode == "preserve":
            validate_preserved_media(source_probe, output_probe)
            if encoded_audio_packet_hashes(ffprobe, output_path) != source_packet_hashes:
                raise RestorationError("preserved output changed encoded audio packets")
        else:
            validate_restored_media(source_probe, output_probe)

    if destination.exists() and not overwrite:
        restored_probe = probe_media(ffprobe, destination)
        validate_output(restored_probe, destination)
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
            validate_output(restored_probe, destination)
            return "already-restored"

        if decision.mode == "preserve":
            run_checked(preservation_command(ffmpeg, source, temporary), timeout=1_800)
        else:
            assert plan is not None  # Keeps static analyzers honest about the branch above.
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
        validate_output(restored_probe, temporary)
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
    argument_parser.add_argument("--repair-clicks", action="store_true", help="opt in to adeclick/adeclip for a recording with confirmed click/clipping defects")
    argument_parser.add_argument("--denoise", action="store_true", help="opt in to gentle adaptive FFT denoising for a recording with confirmed steady noise")
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
        if args.dry_run:
            source_probe = probe_media(ffprobe, source)
            decision = audio_mastering_decision(source_probe)
            details: dict[str, Any] = {
                "source": str(source),
                "output": str(destination),
                "audio_mode": decision.mode,
                "reason": decision.reason,
            }
            if decision.mode == "preserve":
                details["render_command"] = preservation_command(ffmpeg, source, destination)
            else:
                plan = plan_for(
                    source,
                    destination,
                    available_filters=available,
                    mains_hz=args.mains_hz,
                    repair_clicks=args.repair_clicks,
                    denoise=args.denoise,
                )
                details.update({
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
                })
            print(json.dumps(details, indent=2))
            return 0

        state = restore(
            source,
            destination,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            available_filters=available,
            mains_hz=args.mains_hz,
            repair_clicks=args.repair_clicks,
            denoise=args.denoise,
            overwrite=args.overwrite,
        )
        source_probe = probe_media(ffprobe, source)
        decision = audio_mastering_decision(source_probe)
        print(json.dumps({
            "status": state,
            "source": str(source),
            "output": str(destination),
            "profile": (
                "encoded-audio-passthrough-v1"
                if decision.mode == "preserve"
                else "speech-safe-offline-v1"
            ),
            "audio_mode": decision.mode,
            "reason": decision.reason,
        }))
        return 0
    except RestorationError as exc:
        print(f"audio restoration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
