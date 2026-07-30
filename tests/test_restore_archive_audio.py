"""Fast contracts for the offline archive-audio restoration utility."""

from __future__ import annotations

import unittest
from pathlib import Path

from restore_archive_audio import (
    REQUIRED_FILTERS,
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
    restoration_filters,
    validate_restored_media,
)


ALL_FILTERS = set(REQUIRED_FILTERS) | {"adeclick", "adeclip", "afftdn"}


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
