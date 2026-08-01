"""Fast FFmpeg contracts for raw archive-playback audio preservation."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from restore_archive_audio import encoded_audio_packet_hashes
from sync_archive_playback_audio import sync_command, sync_playback_audio


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required for archive media tests",
)
class PlaybackAudioSyncTests(unittest.TestCase):
    def _create_media(self, path: Path, *, tone_hz: int) -> None:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=48x48:rate=8",
                "-f", "lavfi", "-i", f"sine=frequency={tone_hz}:sample_rate=16000",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "16000", "-ac", "1", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

    def test_command_copies_the_original_audio_without_filters(self) -> None:
        command = sync_command(
            "/usr/local/bin/ffmpeg", Path("/tmp/playback.mp4"), Path("/tmp/original.mp4"), Path("/tmp/output.mp4")
        )
        self.assertEqual(command[command.index("-map") + 1], "0:v?")
        self.assertIn("1:a:0", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertNotIn("-af", command)
        self.assertNotIn("-ar", command)
        self.assertNotIn("-ac", command)

    def test_divergent_playback_is_repaired_to_exact_original_audio_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "recording.original.mp4"
            playback = root / "recording.mp4"
            self._create_media(original, tone_hz=440)
            self._create_media(playback, tone_hz=880)
            source_packets = encoded_audio_packet_hashes("ffprobe", original)
            self.assertNotEqual(source_packets, encoded_audio_packet_hashes("ffprobe", playback))

            self.assertEqual(
                sync_playback_audio(playback, original, ffmpeg="ffmpeg", ffprobe="ffprobe"),
                "repaired",
            )
            self.assertEqual(source_packets, encoded_audio_packet_hashes("ffprobe", playback))
            self.assertEqual(
                sync_playback_audio(playback, original, ffmpeg="ffmpeg", ffprobe="ffprobe"),
                "unchanged",
            )

    def test_silent_legacy_original_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "recording.original.mp4"
            playback = root / "recording.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=48x48:rate=8", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(original),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            self._create_media(playback, tone_hz=440)
            before = playback.read_bytes()

            self.assertEqual(
                sync_playback_audio(playback, original, ffmpeg="ffmpeg", ffprobe="ffprobe"),
                "no-original-audio",
            )
            self.assertEqual(playback.read_bytes(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
