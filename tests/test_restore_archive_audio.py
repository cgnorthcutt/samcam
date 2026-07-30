"""Fast contracts for the offline archive-audio restoration utility."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from restore_archive_audio import (
    REQUIRED_FILTERS,
    audio_mastering_decision,
    command_path,
    encoded_audio_packet_hashes,
    preservation_command,
    probe_media,
    RestorationError,
    TARGET_AUDIO_BITRATE,
    TARGET_SAMPLE_RATE,
    default_destination,
    loudnorm_apply_filter,
    parse_available_filters,
    parse_loudnorm_measurement,
    plan_for,
    quiet_input_filter,
    render_command,
    restore,
    restoration_filters,
    validate_preserved_media,
    validate_restored_media,
)


ALL_FILTERS = set(REQUIRED_FILTERS) | {"adeclick", "adeclip", "afftdn"}
RAW_VANGUARD_ORIGINALS = (
    Path("/Users/cgn/Downloads/mcp_video-289_singular_display.mov"),
    Path("/Users/cgn/Downloads/IMG_3095.MOV"),
)


def raw_media_integration_enabled() -> bool:
    return (
        os.environ.get("SAMCAM_RUN_MEDIA_INTEGRATION") == "1"
        and all(path.is_file() for path in RAW_VANGUARD_ORIGINALS)
    )


class RestoreArchiveAudioTests(unittest.TestCase):
    def test_filter_listing_accepts_real_ffmpeg_flag_shapes(self) -> None:
        parsed = parse_available_filters(
            " TS. adeclick          A->A       Remove impulsive noise\n"
            " ... aresample         A->A       Resample audio data\n"
            " TSC afftdn            A->A       Denoise audio samples\n"
        )
        self.assertEqual(parsed, {"adeclick", "aresample", "afftdn"})

    def test_speech_safe_chain_includes_supported_repairs_and_safe_band_limits(self) -> None:
        chain, enabled = restoration_filters(ALL_FILTERS)
        self.assertIn("aresample=48000", chain)
        self.assertIn("highpass=f=75", chain)
        self.assertIn("lowpass=f=7200", chain)
        self.assertIn("adeclick", chain)
        self.assertIn("adeclip", chain)
        self.assertIn("afftdn", chain)
        self.assertEqual(enabled, ("adeclick", "adeclip", "afftdn"))

    def test_hum_notches_are_opt_in_and_deterministic(self) -> None:
        plain, _ = restoration_filters(ALL_FILTERS)
        notched, enabled = restoration_filters(ALL_FILTERS, mains_hz=60)
        self.assertNotIn("equalizer=", plain)
        self.assertIn("equalizer=f=120", notched)
        self.assertIn("equalizer=f=180", notched)
        self.assertIn("60Hz-hum-notches", enabled)

    def test_missing_required_filter_fails_before_any_media_write(self) -> None:
        with self.assertRaisesRegex(RestorationError, "loudnorm"):
            restoration_filters({"aresample", "highpass", "lowpass", "alimiter"})

    def test_loudnorm_parser_rejects_missing_or_invalid_measurements(self) -> None:
        with self.assertRaises(RestorationError):
            parse_loudnorm_measurement("no measurement")
        with self.assertRaisesRegex(RestorationError, "input_tp"):
            parse_loudnorm_measurement('{"input_i":"-20","input_lra":"1","input_thresh":"-30","target_offset":"0"}')

    def test_two_pass_loudnorm_uses_measured_values_then_limiter(self) -> None:
        measured = parse_loudnorm_measurement(
            'noise\n{\n'
            '  "input_i" : "-23.12",\n'
            '  "input_lra" : "4.20",\n'
            '  "input_tp" : "-3.10",\n'
            '  "input_thresh" : "-33.50",\n'
            '  "target_offset" : "0.42"\n'
            '}\n'
        )
        chain = loudnorm_apply_filter("highpass=f=75", measured)
        self.assertIn("measured_I=-23.120000", chain)
        self.assertIn("offset=0.420000", chain)
        self.assertEqual(chain.count("alimiter="), 1)
        self.assertTrue(chain.endswith("latency=1"))

    def test_silent_valid_recording_uses_the_limiter_without_invalid_loudness_gain(self) -> None:
        chain = quiet_input_filter("highpass=f=75,lowpass=f=7200")
        self.assertNotIn("loudnorm", chain)
        self.assertIn("alimiter=limit=0.89", chain)
        self.assertTrue(chain.endswith("latency=1"))

    def test_render_command_stream_copies_video_and_only_reencodes_audio(self) -> None:
        command = render_command(
            "/usr/local/bin/ffmpeg",
            Path("/tmp/input.mp4"),
            Path("/tmp/output.mp4"),
            "highpass=f=75,alimiter=limit=0.89",
        )
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], TARGET_AUDIO_BITRATE)
        self.assertEqual(command[command.index("-ar") + 1], str(TARGET_SAMPLE_RATE))
        self.assertIn("+faststart", command)
        self.assertIn("+bitexact", command)
        self.assertIn("0:v?", command)
        self.assertIn("0:a:0", command)

    def test_healthy_stereo_source_uses_lossless_audio_passthrough(self) -> None:
        source_probe = {
            "format": {"duration": "73.3"},
            "streams": [{
                "codec_type": "video", "codec_name": "hevc", "codec_tag_string": "hvc1",
                "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
            }, {
                "codec_type": "audio", "codec_name": "aac", "profile": "LC",
                "sample_rate": "48000", "channels": 2, "channel_layout": "stereo",
                "bit_rate": "96374",
            }],
        }
        decision = audio_mastering_decision(source_probe)
        self.assertEqual(decision.mode, "preserve")

        command = preservation_command(
            "/usr/local/bin/ffmpeg", Path("/tmp/input.mov"), Path("/tmp/output.mp4")
        )
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertNotIn("-af", command)
        self.assertNotIn("-ar", command)
        self.assertNotIn("-ac", command)

    def test_degraded_mono_bodycam_audio_still_uses_restoration(self) -> None:
        bodycam_probe = {
            "format": {"duration": "76.4"},
            "streams": [{
                "codec_type": "audio", "codec_name": "aac", "profile": "LC",
                "sample_rate": "16000", "channels": 1, "channel_layout": "mono",
                "bit_rate": "52940",
            }],
        }
        decision = audio_mastering_decision(bodycam_probe)
        self.assertEqual(decision.mode, "restore")
        self.assertIn("sample rate", decision.reason)

    def test_default_destination_never_overwrites_source(self) -> None:
        source = Path("/archive/recording.mp4")
        self.assertEqual(default_destination(source), Path("/archive/recording.restored.mp4"))

    def test_plan_is_stable_and_has_no_live_capture_dependencies(self) -> None:
        plan = plan_for(
            Path("/archive/recording.mp4"),
            Path("/archive/recording.restored.mp4"),
            available_filters=ALL_FILTERS,
            mains_hz=0,
            repair_clicks=True,
            denoise=True,
        )
        self.assertEqual(plan.enabled_optional_filters, ("adeclick", "adeclip", "afftdn"))
        self.assertNotIn("avfoundation", plan.first_pass_filter)
        self.assertNotIn("websocket", plan.first_pass_filter)

    def test_media_validation_requires_unchanged_video_and_expected_aac(self) -> None:
        source = {
            "format": {"duration": "4.0"},
            "streams": [{
                "codec_type": "video", "codec_name": "h264", "codec_tag_string": "avc1",
                "width": 1280, "height": 720, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
            }, {"codec_type": "audio", "codec_name": "aac"}],
        }
        restored = {
            "format": {"duration": "4.1"},
            "streams": [{
                "codec_type": "video", "codec_name": "h264", "codec_tag_string": "avc1",
                "width": 1280, "height": 720, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
            }, {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 1}],
        }
        validate_restored_media(source, restored)

        restored["streams"][0]["codec_name"] = "hevc"
        with self.assertRaisesRegex(RestorationError, "video"):
            validate_restored_media(source, restored)

    def test_preserved_media_rejects_any_audio_metadata_change(self) -> None:
        source = {
            "format": {"duration": "4.0"},
            "streams": [{
                "codec_type": "video", "codec_name": "hevc", "codec_tag_string": "hvc1",
                "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
            }, {
                "codec_type": "audio", "codec_name": "aac", "profile": "LC",
                "sample_rate": "48000", "channels": 2, "channel_layout": "stereo",
            }],
        }
        preserved = {
            "format": {"duration": "4.0"},
            "streams": [{
                "codec_type": "video", "codec_name": "hevc", "codec_tag_string": "hvc1",
                "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
            }, {
                "codec_type": "audio", "codec_name": "aac", "profile": "LC",
                "sample_rate": "48000", "channels": 1, "channel_layout": "mono",
            }],
        }
        with self.assertRaisesRegex(RestorationError, "source audio metadata"):
            validate_preserved_media(source, preserved)

    @unittest.skipUnless(
        raw_media_integration_enabled(),
        "set SAMCAM_RUN_MEDIA_INTEGRATION=1 with the original Vanguard MOVs present",
    )
    def test_raw_vanguard_originals_are_stream_copied_packet_for_packet(self) -> None:
        """Opt-in proof against the actual original, not a cached archive MP4."""
        ffmpeg = command_path("ffmpeg")
        ffprobe = command_path("ffprobe")
        with tempfile.TemporaryDirectory(prefix="samcam-audio-preservation-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            for source in RAW_VANGUARD_ORIGINALS:
                with self.subTest(source=source.name):
                    source_probe = probe_media(ffprobe, source)
                    self.assertEqual(audio_mastering_decision(source_probe).mode, "preserve")
                    destination = temporary_root / f"{source.stem}.preserved.mp4"
                    self.assertEqual(
                        restore(
                            source,
                            destination,
                            ffmpeg=ffmpeg,
                            ffprobe=ffprobe,
                            available_filters=(),
                        ),
                        "created",
                    )
                    validate_preserved_media(source_probe, probe_media(ffprobe, destination))
                    self.assertEqual(
                        encoded_audio_packet_hashes(ffprobe, source),
                        encoded_audio_packet_hashes(ffprobe, destination),
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
