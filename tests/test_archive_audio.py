"""End-to-end FFmpeg contract for archived USB-camera audio."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from publish_worker import (
    ARCHIVE_AUDIO_BYTES_PER_SECOND,
    LocalSegment,
    SessionArchiver,
)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required for archive media tests",
)
class ArchiveAudioTests(unittest.TestCase):
    def _probe_streams(self, path: Path) -> set[tuple[str, str]]:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        streams = json.loads(probe.stdout)
        return {
            (stream["codec_type"], stream["codec_name"])
            for stream in streams.get("streams", [])
        }

    def _probe_audio(self, path: Path) -> dict[str, str]:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        self.assertEqual(len(streams), 1)
        return streams[0]

    def test_part_preview_and_stitched_recording_contain_aac_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary) / "Curtis-audio-contract-0001"
            session_dir.mkdir()
            raw_path = session_dir / "segment-00000.mjpeg"
            generated = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=32x32:r=5", "-frames:v", "5",
                    "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            raw_path.write_bytes(generated.stdout)
            raw_path.with_suffix(".s16le").write_bytes(
                b"\0" * ARCHIVE_AUDIO_BYTES_PER_SECOND
            )

            archiver = SessionArchiver(Path(temporary))
            part = LocalSegment(
                session_id=session_dir.name,
                sequence=0,
                started_at=1_700_000_000,
                duration_seconds=1.0,
                path=session_dir / "segment-00000.mp4",
                frame_count=5,
            )
            archiver._encode_segment(raw_path, part)
            self.assertTrue(part.path.exists())
            self.assertIn(("audio", "aac"), self._probe_streams(part.path))
            self.assertIn(("video", "h264"), self._probe_streams(part.path))

            # The relay-safe in-progress copy must keep audio too.
            archiver._encode_preview(part.path, 1.0)
            preview = archiver._preview_path(part.path)
            self.assertTrue(preview.exists())
            self.assertIn(("audio", "aac"), self._probe_streams(preview))

            # Two same-layout parts can be concat-copied without dropping AAC.
            second = session_dir / "segment-00001.mp4"
            shutil.copyfile(part.path, second)
            archiver._stitch_recording({"session_id": session_dir.name})
            recording = session_dir / "recording.mp4"
            original_recording = session_dir / "recording.original.mp4"
            self.assertTrue(recording.exists())
            self.assertTrue(original_recording.exists())
            self.assertIn(("audio", "aac"), self._probe_streams(recording))
            self.assertIn(("audio", "aac"), self._probe_streams(original_recording))
            # The A/B source remains the unmastered concat output. The raw
            # body-camera AAC is 16 kHz mono while the primary archive MP4 is
            # the separately mastered, 48 kHz playback version.
            original_audio = self._probe_audio(original_recording)
            self.assertEqual(original_audio["sample_rate"], "16000")
            self.assertEqual(original_audio["channels"], 1)
            # The final MP4, unlike a relay-safe preview, is mastered after
            # stitching: it preserves copied video and uses 48 kHz AAC audio.
            mastered_audio = self._probe_audio(recording)
            self.assertEqual(mastered_audio["codec_name"], "aac")
            self.assertEqual(mastered_audio["sample_rate"], "48000", archiver.errors)
            self.assertEqual(mastered_audio["channels"], 1)

    def test_live_pcm_is_written_with_the_active_part_before_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archiver = SessionArchiver(Path(temporary))
            session = archiver.start("Curtis", "LIVE · GENERAL - UVC")
            pcm = b"\x01\0" * 800
            try:
                archiver.write_audio(pcm)
                assert archiver.active_audio is not None
                archiver.active_audio.flush()

                raw_audio = Path(temporary) / session["session_id"] / "segment-00000.s16le"
                self.assertEqual(raw_audio.read_bytes()[-len(pcm):], pcm)
            finally:
                archiver.stop()

    def test_delayed_audio_arrival_never_inserts_mid_recording_silence(self) -> None:
        """Network bursts are not a reason to cut a hole in a spoken sentence."""
        with tempfile.TemporaryDirectory() as temporary:
            archiver = SessionArchiver(Path(temporary))
            session = archiver.start("Curtis", "LIVE · GENERAL - UVC")
            try:
                assert archiver.active_segment_started_at is not None
                # Simulate a packet delayed in HTTP scheduling. Its PCM must
                # remain contiguous rather than being preceded by fabricated
                # wall-clock silence.
                archiver.active_segment_started_at -= 2.0
                pcm = b"\x10\x00" * 800
                archiver.write_audio(pcm)
                assert archiver.active_audio is not None
                archiver.active_audio.flush()
                raw_audio = Path(temporary) / session["session_id"] / "segment-00000.s16le"
                self.assertEqual(raw_audio.read_bytes(), pcm)
            finally:
                archiver.stop()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
