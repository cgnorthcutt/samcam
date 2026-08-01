"""Contract tests for a user-approved archive soundtrack replacement."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from apply_reference_audio import apply_reference_audio, extract_audio_command, remux_command
from restore_archive_audio import encoded_audio_packet_hashes


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
class ApplyReferenceAudioTests(unittest.TestCase):
    def make_media(self, destination: Path, *, tone: int) -> None:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=48x48:rate=8",
                "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(destination),
            ],
            check=True, capture_output=True, timeout=30,
        )

    def test_commands_stream_copy_audio_and_camera_video(self) -> None:
        extraction = extract_audio_command("ffmpeg", Path("camera.mp4"), Path("audio.m4a"))
        remux = remux_command("ffmpeg", Path("camera.mp4"), Path("reference.mp4"), Path("playback.mp4"))
        self.assertEqual(extraction[extraction.index("-c:a") + 1], "copy")
        self.assertEqual(remux[remux.index("-c:v") + 1], "copy")
        self.assertEqual(remux[remux.index("-c:a") + 1], "copy")
        self.assertNotIn("-af", remux)

    def test_reference_audio_replaces_playback_and_both_tracks_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            session.mkdir()
            camera = session / "recording.original.mp4"
            reference = Path(temporary) / "reference.mp4"
            self.make_media(camera, tone=440)
            self.make_media(reference, tone=880)
            expected_original = encoded_audio_packet_hashes("ffprobe", camera)
            expected_fixed = encoded_audio_packet_hashes("ffprobe", reference)

            apply_reference_audio(session, reference, ffmpeg="ffmpeg", ffprobe="ffprobe")

            self.assertEqual(expected_original, encoded_audio_packet_hashes("ffprobe", session / "audio.bodycam-original.m4a"))
            self.assertEqual(expected_fixed, encoded_audio_packet_hashes("ffprobe", session / "audio.fixed-reference.m4a"))
            self.assertEqual(expected_fixed, encoded_audio_packet_hashes("ffprobe", session / "recording.mp4"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
