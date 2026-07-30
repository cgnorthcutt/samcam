"""Fast media contracts for the supplied Meta Oakley Vanguard demonstrations.

The original MOVs are intentionally not committed: together they are over
300 MB.  A developer machine that has them in ``sources/`` or ``~/Downloads``
can run this test to validate the real assets.  CI and clones without those
private files skip clearly rather than substituting synthetic audio.

The browser-compatible files in ``.cache`` provide H.264 video only.  This
contract ensures archive import uses AAC packets from the original MOV rather
than applying a lossy audio pass or downmix.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from import_demo_archives import DEMOS, merge_browser_video_with_original_audio


FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
AUDIO_LAYOUT_FIELDS = ("codec_name", "profile", "sample_rate", "channels", "channel_layout")


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


def pcm_hash(path: Path) -> str:
    """Hash decoded PCM as an independent proof that playback is unchanged."""
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
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return hashlib.sha256(result.stdout).hexdigest()


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

    def test_archive_import_preserves_raw_aac_packets_and_stereo(self) -> None:
        """Archive import must use raw audio, not the lower-bitrate cache.

        A packet hash is stricter than matching sample rate/channels: it
        catches AAC re-encoding even if a replacement retains stereo at 48 kHz.
        The generated output also remains browser-playable because only its
        H.264 video comes from the existing browser cache.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for demo in DEMOS:
                with self.subTest(demo=demo.original_filename):
                    archive = root / f"{demo.session_id}.mp4"
                    raw = self.raw_sources[demo.session_id]
                    self.assertTrue(demo.converted_path.is_file(), f"missing browser video: {demo.converted_path}")
                    merge_browser_video_with_original_audio(demo.converted_path, raw, archive)
                    self.assertTrue(archive.is_file(), "import did not create an archive MP4")
                    raw_audio = probe_audio(raw)
                    archive_audio = probe_audio(archive)
                    self.assertEqual(
                        {field: archive_audio[field] for field in AUDIO_LAYOUT_FIELDS},
                        {field: raw_audio[field] for field in AUDIO_LAYOUT_FIELDS},
                        "archive import must not re-encode or downmix the raw Vanguard audio",
                    )
                    self.assertEqual(packet_hash(archive), packet_hash(raw))
                    self.assertEqual(pcm_hash(archive), pcm_hash(raw))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
