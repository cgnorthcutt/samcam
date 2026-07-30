# Meta Oakley Vanguard demo audio preservation

The two supplied Meta Oakley Vanguard AI Glasses originals are high-quality
stereo captures and should never be put through the USB-body-camera speech
repair profile.  That profile is for the known 16 kHz mono body-camera defect,
not a universal audio enhancer.

## Source measurements

The fast integration test probes the original private files when they are
present in either `sources/` or `~/Downloads`:

| Original | Capture duration | Audio |
| --- | ---: | --- |
| `mcp_video-289_singular_display.mov` | 73.300 s | AAC-LC, 48 kHz, stereo, about 128 kb/s |
| `IMG_3095.MOV` | 72.682 s | AAC-LC, 48 kHz, stereo, about 254 kb/s |

These measurements are from the actual supplied files, rather than an assumed
device marketing specification.  Their two audio channels and 48 kHz sampling
rate are the quality gate for this demo source class.

## What the archive test protects

Run the real-media contract locally:

```bash
.venv/bin/python -m unittest tests.test_vanguard_demo_audio_preservation -v
```

It skips clearly when the private originals are absent.  When they are
available, it verifies that:

1. both original MOVs are 48 kHz stereo AAC and exceed 100 kb/s;
2. the saved archive MP4 has the same audio stream layout as its browser-ready
   upload master; and
3. the compressed AAC packets have identical SHA-256 hashes.  This catches an
   archive re-encode even when a replacement keeps the same sample rate and
   channel count.

The existing demo archive copies are therefore bit-for-bit audio-preserving
relative to their `*.cache/*.mp4` upload masters.  The upload masters were
created before archive import and are 96 kb/s stereo AAC; they are **not**
bit-for-bit copies of the original MOV audio.  The test makes that provenance
explicit so an archive regression is not mistaken for the earlier browser
conversion.

## Policy

For known-good 48 kHz stereo input, preserve the existing encoded audio stream
(`-c:a copy`) and retain both channels.  Do not apply denoise, de-click,
de-clip, loudness normalization, sample-rate conversion, mono downmixing, or
hum notches unless a separate diagnostic explicitly identifies a defect and a
user elects to repair a derived copy.

The automatic speech-mastering path currently targets recordings captured by
the USB body-camera pipeline.  A future high-fidelity capture path must make
this quality gate executable *before* it invokes that mastering helper; the
real-demo contract above is the regression fixture for that work.  Until such
a gate exists, import high-quality clips through the direct archive-copy path
used by `import_demo_archives.py`.
