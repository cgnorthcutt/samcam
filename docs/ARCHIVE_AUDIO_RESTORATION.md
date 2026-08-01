# Offline archive audio restoration

> Current archive playback deliberately uses the camera's original captured
> audio. Do not publish a `recording.restored.mp4` as the default viewer source.
> To repair an older playback MP4 from its retained original camera-audio
> packets without applying any DSP, run
> `python3 sync_archive_playback_audio.py archives` instead. It validates both
> the copied AAC packet hashes and copied video packet hashes before replacing
> a local playback file.

`restore_archive_audio.py` is a reusable **offline-only** post-processing step
for completed Ego Capture recordings. It does not run in the browser, does not
touch `cloud/static`, and is never used by the live capture or WebSocket relay.
Use it after a session has ended when the final playback needs the clearest
possible speech track.

This is a best-effort repair pass, not a way to recreate samples that were
already destroyed by microphone clipping or the camera's original AAC encode.
It safely reduces the audible consequences of those faults and prevents new
clipping in the exported MP4; clean source capture is still the ceiling.

## Safety and idempotence

- The source `recording.mp4` is read only.
- The default result is a sibling named `recording.restored.mp4`.
- A unique temporary MP4 is probed before an atomic rename creates the final
  file. Partial work is cleaned on success, error, timeout, or interruption.
- An existing valid result is returned as `already-restored`; it is not
  reprocessed. Use `--overwrite` only when deliberately replacing it.
- A per-output lock prevents two restoration processes from writing the same
  recording concurrently.
- Validation requires every original video stream to retain its identity and
  requires exactly one 48 kHz mono AAC output stream. Video is `-c:v copy`.

## Pipeline

The conservative default prioritizes intelligible voice over aggressive noise
removal:

1. SoX-quality resampling to 48 kHz.
2. 75 Hz high-pass for DC, handling rumble, and low mains hum; 7.2 kHz
   low-pass for harsh high-frequency feedback/noise.
3. `adeclick`, `adeclip`, and `afftdn` when the installed FFmpeg provides
   them. The helper discovers support with `ffmpeg -filters`; missing optional
   filters are skipped, while missing core filters fail before media is
   written.
4. Two-pass EBU R128 `loudnorm` targeting -16 LUFS / -2 dBTP, followed by a
   conservative peak limiter.
5. AAC-LC at 96 kb/s, 48 kHz mono; video and subtitles are stream copied.

For a clearly steady electrical hum, add narrow optional harmonic notches:

```bash
python3 restore_archive_audio.py archives/SESSION_ID/recording.mp4 --mains-hz 60
```

Do not turn on the hum option for ordinary room noise. The normal high-pass and
adaptive denoise are safer for speech.

## Commands

```bash
# Inspect the chosen filters and both commands; writes no media.
python3 restore_archive_audio.py archives/SESSION_ID/recording.mp4 --dry-run

# Create recording.restored.mp4, preserving recording.mp4.
python3 restore_archive_audio.py archives/SESSION_ID/recording.mp4

# Retry after a deliberate earlier output, preserving atomic replacement.
python3 restore_archive_audio.py archives/SESSION_ID/recording.mp4 --overwrite
```

The utility needs `ffmpeg` and `ffprobe` on `PATH`. It exits nonzero without
touching the source if FFmpeg lacks a required filter, loudness measurement
fails, encoding fails, audio/video validation fails, or another restoration is
already running for the same output.

## Verification

The fast contract tests do not process real footage:

```bash
python3 -m unittest tests.test_restore_archive_audio
```

They verify command construction, filter fallback, two-pass measurement
handling, AAC output requirements, and invariant video stream copying. For a
specific recording, run the helper with `--dry-run` first, then listen to the
restored MP4 on headphones before using it in a presentation.
