"""Fast media contracts for the supplied Meta Oakley Vanguard demonstrations.

The original MOVs are intentionally not committed: together they are over
300 MB.  A developer machine that has them in ``sources/`` or ``~/Downloads``
can run this test to validate the real assets.  CI and clones without those
private files skip clearly rather than substituting synthetic audio.

The browser-compatible files in ``.cache`` were made before Sam Cam imported
them.  This contract ensures archive import never applies another lossy audio
pass or downmix: its saved MP4 must have the same AAC packet hash and audio
stream layout as that upload master.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from import_demo_archives import DEMOS, Demo


FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")


def probe_audio(path: Path) -> dict[str, object]:
    """Return the primary audio stream as FFprobe reports it."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,profile,sample_rate,channels,channel_layout,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if len(streams) != 1:
        raise AssertionError(f"expected exactly one audio stream in {path}")
    return streams[0]


def packet_hash(path: Path) -> str:
    """Hash compressed AAC packets without decoding or re-encoding them."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    prefix = "SHA256="
    output = result.stdout.strip()
    if not output.startswith(prefix):
        raise AssertionError(f"FFmpeg did not emit an AAC packet hash for {path}: {output!r}")
    return output.removeprefix(prefix)


@unittest.skipUnless(FFPROBE and FFMPEG, "ffmpeg and ffprobe are required for real-demo audio checks")
class VanguardDemoAudioPreservationTests(unittest.TestCase):
    """Protect the high-quality two-channel demo import path from regressions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_sources: dict[str, Path] = {}
        missing: list[str] = []
        for demo in DEMOS:
            source = next((candidate for candidate in demo.original_candidates() if candidate.is_file()), None)
            if source is None:
                missing.append(demo.original_filename)
            else:
                cls.raw_sources[demo.session_id] = source
        if missing:
            raise unittest.SkipTest(
                "Meta Oakley Vanguard originals are not present in sources/ or ~/Downloads: "
                + ", ".join(missing)
            )

    @staticmethod
    def _archive_recording(demo: Demo) -> Path:
        return Path(__file__).resolve().parents[1] / "archives" / demo.session_id / "recording.mp4"

    def test_raw_vanguard_sources_are_48khz_stereo_aac(self) -> None:
        """Confirm the actual supplied sources meet the high-fidelity gate."""
        for demo in DEMOS:
            with self.subTest(demo=demo.original_filename):
                audio = probe_audio(self.raw_sources[demo.session_id])
                self.assertEqual(audio["codec_name"], "aac")
                self.assertEqual(audio["sample_rate"], "48000")
                self.assertEqual(audio["channels"], 2)
                self.assertEqual(audio["channel_layout"], "stereo")
                # Both real files are materially above the current 96 kb/s
                # browser master.  Keep the assertion deliberately broad so
                # it describes a quality class, not one encoder's exact rate.
                self.assertGreater(int(audio["bit_rate"]), 100_000)

    def test_archive_import_preserves_upload_master_aac_packets_and_stereo(self) -> None:
        """Archive import must copy, not remaster, either supplied demo.

        A packet hash is stricter than matching sample rate/channels: it
        catches AAC re-encoding even if a replacement retains stereo at 48 kHz.
        """
        for demo in DEMOS:
            with self.subTest(demo=demo.original_filename):
                archive = self._archive_recording(demo)
                self.assertTrue(archive.is_file(), f"missing imported demo: {archive}")
                self.assertTrue(demo.converted_path.is_file(), f"missing upload master: {demo.converted_path}")
                master_audio = probe_audio(demo.converted_path)
                archive_audio = probe_audio(archive)
                self.assertEqual(
                    archive_audio,
                    master_audio,
                    "archive import must not re-encode or downmix the demo upload master",
                )
                self.assertEqual(packet_hash(archive), packet_hash(demo.converted_path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
