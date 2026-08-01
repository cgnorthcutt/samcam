# Egocentric Camera Lab

An independent experiment in connecting USB and wearable cameras to a simple
browser-based live capture app. It explores how an everyday camera can become
a live, remotely viewable point-of-view feed with local transcription, saved
sessions, and lightweight video analysis.

This is a personal hardware/software project. It is not affiliated with or
endorsed by any employer, camera maker, or platform.

## What it does

- Detects a compatible macOS UVC video and audio device.
- Shows the selected camera locally, without falling back to the laptop webcam
  or phone camera.
- Sends a live JPEG/video-and-audio relay through one outbound connection.
- Saves completed recordings, transcript lines, and per-video visual analysis.
- Lets a separate browser view the current feed or previous sessions.

The reference device is a compact USB body camera, but the project is intended
for experimentation with any source macOS exposes as a UVC-style camera. Two
example clips from wearable glasses are included to exercise a second capture
format; their original capture times are retained when imported.

## Architecture

```text
camera → local capture app → outbound publisher → web relay → viewers
            └─ local archive ────────────────┴─ durable session archive
```

The camera remains connected to the local laptop. Remote viewers never access
the camera directly. A published live stream needs the local capture app and
publisher to stay running; already-uploaded recordings can be served by the
relay independently.

## Run locally

Install FFmpeg and build the macOS capture helper:

```bash
brew install ffmpeg
swiftc -O bodycam_capture.swift -o bodycam_capture \
  -framework AVFoundation -framework CoreImage -framework CoreMedia \
  -framework CoreVideo -framework AudioToolbox
```

Create the local environments:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-transcription.txt
uv pip install --python .venv/bin/python -r requirements-publisher.txt
```

Start the local app:

```bash
python3 server.py 8011
```

Open [http://localhost:8011](http://localhost:8011). The optional local
transcription worker downloads its model on the first run.

## Publish to your own relay

The project intentionally contains no production host name, account, or
credential. Point the publisher at a relay you control:

```bash
EGOCAPTURE_RELAY_URL=https://<your-relay-host> \
EGOCAPTURE_WORKER="camera-lab" \
.venv/bin/python publish_worker.py
```

The publisher only creates an outbound connection. Do not put API keys,
database URLs, or personal recordings in Git.

`render.yaml` is an optional deployment blueprint for a single Python relay.
Set `DATABASE_URL` in the host's private environment if you want durable
archival storage. Live state is intentionally held by one relay process;
horizontally scaling it needs shared live-state infrastructure.

## Camera mode flow

Many compact cameras appear as USB storage when powered off and re-enumerate as
UVC video plus audio after pressing Record. macOS can take several seconds to
recognize that mode change. The local app clears stale live frames when the
camera disconnects or returns to storage mode rather than substituting an old
recording.

## Archive and analysis

Sessions are temporarily saved as playable parts while recording. A background
job combines completed parts into one MP4 without blocking playback. The final
archive uses the camera's original audio track. Transcript lines are assistive
and may reject weak, noisy, repetitive, or low-confidence speech.

For legacy local sessions whose playback MP4 diverged from the retained camera
track, run the packet-preserving repair after capture has stopped:

```bash
python3 sync_archive_playback_audio.py archives
```

It uses FFmpeg `-c:a copy` and verifies encoded audio packet hashes before an
atomic replacement. Silent recordings are reported and never modified.

Share the archive directly at `https://<your-relay-host>/archive` (or
`/#archive`).

Per-video analysis is exploratory—not device telemetry, medical guidance,
market research, or measured ergonomics. It combines image-derived motion and
lighting samples with clearly marked assumptions.

## Tests

Run the fast local suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The test suite uses local fixtures and in-memory archive contracts. It does
not require a connected camera, hosted account, or network access.

## Project layout

```text
server.py                 local camera discovery, capture, transcript, and UI
bodycam_capture.swift     AVFoundation video/audio helper
publish_worker.py         outbound relay publisher and session archiver
cloud/main.py             relay, archive API, and analysis API
cloud/static/             public browser interface
transcribe_worker.py      optional local MLX Whisper worker
docs/                     generic operating and validation notes
```
