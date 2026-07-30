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
2. a fresh browser-ready archive MP4 preserves the raw original's AAC-LC
   codec, 48 kHz sample rate, and stereo channel layout; and
3. both the compressed AAC packet stream **and** decoded PCM have identical
   SHA-256 hashes to the original MOV.  This catches an archive re-encode,
   downmix, resample, trim, or DSP pass even when a replacement happens to
   retain the same visible stream layout.

The browser-compatible `*.cache/*.mp4` files are H.264 video sources only;
they were created before archive import and contain lower-bitrate 96 kb/s AAC.
When an original MOV is present, the importer takes cache video but copies the
original AAC packets into the final archive MP4.  If the private original is
not available in a clone, it leaves the cache as a playable fallback and does
not claim that fallback is source-lossless.

## Policy

For known-good 48 kHz stereo input, preserve the existing encoded audio stream
(`-c:a copy`) and retain both channels.  Do not apply denoise, de-click,
de-clip, loudness normalization, sample-rate conversion, mono downmixing, or
hum notches unless a separate diagnostic explicitly identifies a defect and a
user elects to repair a derived copy.

The automatic speech-mastering path targets recordings captured by the USB
body-camera pipeline.  Its executable quality gate runs before mastering:
known-good high-fidelity stereo inputs use audio stream-copy, while the known
16 kHz mono body-camera profile remains eligible for measured repair.  The
real-demo contract above is the regression fixture for this distinction.
