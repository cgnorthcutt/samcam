# Sam Cam

[**Open the public demo at samcam.app →**](https://samcam.app)

Sam Cam turns a compatible USB body camera into a local and public live view
with live transcription, saved sessions, and per-video customer-fit analytics.
The reference device is the [Mini Body Camera B08KY7KLPB](https://www.amazon.com/dp/B08KY7KLPB).
It captures only the selected USB camera's video and audio—it never silently
falls back to the Mac camera, phone, or another webcam.

> **Final-demo operator guide:** see [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md).
> It is the short, step-by-step checklist for starting, verifying, and
> recovering the demo.

## What is running where

```text
USB camera → local Sam Cam → local publisher ──outbound WSS──> Render relay → samcam.app viewers
                   │                                      │
                   └─ local session parts / final MP4 ────┴─ Postgres archive + analytics
```

- The USB camera must remain connected to the worker laptop. Public viewers do
  not access the camera directly.
- The local app owns the camera and sends JPEG video, body-camera audio, and
  local Whisper transcript lines to the publisher.
- The Render relay serves the public Live, Archive, and Analytics views.
  Archived sessions remain available when the laptop is off **after** their
  media, transcript, and analytics have finished uploading.
- Live video is deliberately served by one relay instance: current frames and
  short-lived live-audio packets are in memory, so it must not be scaled across
  multiple web instances without shared live-state infrastructure.

## Camera compatibility

Sam Cam can work with another source when macOS exposes it as a compatible
UVC-style video-and-audio device. The USB body camera above is the supported
and validated demo device; other cameras may need different capture settings.

The archive includes two clearly labeled example clips captured with previously
hacked **Meta Oakley Vanguard AI Glasses**. Their display times come from the
original MOV files' QuickTime capture metadata—not from the later import or
transcode time.

## Install and run locally

Install FFmpeg:

```bash
brew install ffmpeg
```

Build the native capture helper:

```bash
swiftc -O bodycam_capture.swift -o bodycam_capture \
  -framework AVFoundation -framework CoreImage -framework CoreMedia \
  -framework CoreVideo -framework AudioToolbox
```

Create the local transcription/publisher environment:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-transcription.txt
uv pip install --python .venv/bin/python -r requirements-publisher.txt
```

Start the local camera app on the usual demo port:

```bash
python3 server.py 8011
```

Open [http://localhost:8011](http://localhost:8011). The first transcription
run downloads `mlx-community/whisper-large-v3-turbo`; later runs use its local
model cache.

For the public demo, start the publisher in a second terminal after the local
app is running:

```bash
SAMCAM_RELAY_URL=https://samcam.app \
SAMCAM_WORKER=Curtis \
.venv/bin/python publish_worker.py
```

The publisher makes an outbound connection only; no port forwarding or public
IP address is needed. Do not put API keys, database URLs, or other credentials
in source files, commands recorded in a demo, or Git.

## Camera flow

The reference camera has two USB personalities:

1. Connect the powered-off camera by USB. It appears as **storage mode**; its
   recorded clips can be browsed locally.
2. While it remains connected, short-press the camera's Record/ON-OFF button.
3. It re-enumerates as `GENERAL - UVC` plus `GENERAL - AUDIO`.
4. Sam Cam detects UVC mode, begins the local live view, and the publisher
   makes the named public worker live.

Mode changes can take several seconds while macOS re-enumerates the USB
devices. During that transition, wait for Sam Cam to update rather than
restarting both processes.

When the camera is unplugged or returns to storage mode, Live correctly clears
the old image and shows that the worker is not streaming. It must never replace
that state with archived footage.

## Live video, sound, and transcript

- **Live video** is a shared MJPEG feed sourced only from the USB UVC camera.
  On the public site it is available at `/?worker=Curtis` by default.
- **Sound is opt-in.** A viewer must select **Enable sound** because browsers
  block autoplay with audio. Use headphones for a final demo when practical.
  The browser applies an attenuated, high-pass/low-pass filtered, compressed
  playback path to reduce speaker-to-camera feedback; it cannot guarantee safe
  playback when the camera microphone is next to loud speakers.
- **Live transcription** uses the body-camera microphone and a local MLX
  Whisper model. It is scoped to the current live session only; when the
  session ends, accepted lines move with that saved session into Archive.
  It can show no speech when the model rejects silence, weak speech, feedback,
  repetition, or low-confidence audio. It is a demo aid, not a verbatim or
  safety-critical record.

Useful transcription tuning variables:

```bash
BODYCAM_WHISPER_MODEL=mlx-community/whisper-large-v3-turbo \
BODYCAM_TRANSCRIPT_LANGUAGE=en \
BODYCAM_TRANSCRIPT_CHUNK_SECONDS=3 \
BODYCAM_TRANSCRIPT_MIN_AVG_LOGPROB=-1.0 \
python3 server.py 8011
```

Raising `BODYCAM_TRANSCRIPT_MIN_AVG_LOGPROB` favors fewer false lines;
lowering it accepts more uncertain audio.

## Archive and analytics

Every public live session is also captured locally as MP4 parts with AAC audio.
While a session is active or just ended:

- Parts close roughly every **five minutes** by default
  (`SAMCAM_ARCHIVE_SEGMENT_SECONDS=300`). They can appear as playable parts
  before the full session is ready.
- A background job stitches finished parts into one final MP4. It does not
  block part playback; the browser can switch to the final recording once it
  arrives.
- The publisher uploads the completed MP4 in durable chunks, plus the accepted
  transcript and analytics. Allow this to finish before turning off the laptop
  if the new recording must be available publicly.

Archive is durable only after the Render Postgres-backed relay has accepted the
uploads. If the relay shows **Archive is temporarily unavailable** or
**Reconnecting**, it deliberately preserves the last known list or retries
instead of pretending a durable archive is empty. Live video may still work
while Archive is reconnecting.

To permanently remove an uploaded public session, use the explicit maintenance
tool with the session ID. This deletes the public video, transcript, and
analytics and writes a deletion marker so the connected publisher does not
re-upload it. It intentionally does **not** delete the local `archives/`
folder.

```bash
.venv/bin/python purge_archives.py SESSION_ID
```

Use `--dry-run` first when verifying IDs. Deletion is permanent for the public
relay; keep any local copy you need before requesting it.

The **Analytics** tab appears for completed recordings whose per-video analysis
has uploaded. Its motion and lighting series are sampled from the video; battery
ETA, weight, neck-load, ergonomics, customer-fit scores, and the Pareto view
combine those measurements with stated product assumptions. These are planning
estimates—not device telemetry, medical guidance, market research, or measured
biomechanics.

## Expected public states

| What you see | Meaning | Normal next step |
|---|---|---|
| `Curtis is live` | Fresh camera frames are reaching the relay. | Click **Enable sound** if sound is desired. |
| `Worker Curtis is not streaming at this time` | The camera is unplugged, in storage mode, not recording, or the local publisher has no fresh frame. | Check the local app, then press Record on the connected camera. |
| `Connecting to Curtis's live camera…` | The public page has a fresh live session and is opening the MJPEG feed. | Give it a few seconds; reload once only if it does not resolve. |
| `Archive is temporarily unavailable` / `Reconnecting…` | The durable archive database is unhealthy or reconnecting. | Live can remain available. Check `/healthz`; do not treat this as an empty archive. |
| Public HTTP `503` | The Render web service is not currently serving requests. | Check Render service and database health before restarting the local camera. |

## Render relay

`render.yaml` defines the relay as one Python web service. The archive needs a
Render Postgres instance in the same region, with `DATABASE_URL` set to that
instance's **Internal Database URL**. The health endpoint is:

```bash
curl -fsS https://samcam.app/healthz
```

Healthy archive output includes `"archive":"ready"`. A response with
`"archive":"reconnecting"` means the live relay is deliberately remaining
available while durable archive requests retry; Archive endpoints can return
503 in that state rather than showing a false empty result.

The public demo is intentionally unauthenticated. Anyone who knows a worker
name can view its stream, and anyone can attempt to publish under a worker
name. It is only appropriate for a short, non-private demo. Remove the public
service after the demo.

## Useful files

```text
server.py                 local HTTP server, USB discovery, capture, local UI
bodycam_capture.swift     native AVFoundation video/audio helper
publish_worker.py         outbound public relay publisher and session archiver
cloud/main.py             Render relay, durable archive API, analytics API
cloud/static/index.html   public Live / Archive / Analytics interface
transcribe_worker.py      persistent local MLX Whisper worker
import_demo_archives.py   idempotent Meta Oakley demo metadata/media seeder
purge_archives.py         explicit public archive deletion utility
docs/DEMO_RUNBOOK.md      final-demo checklist and recovery guide
ideas.md                  investigation history and fallback ideas
```

## HTTP endpoints

### Local app

| Route | Purpose |
|---|---|
| `GET /` | Local Sam Cam interface |
| `GET /stream.mjpg` | Live USB body-camera MJPEG feed |
| `GET /api/status` | Camera connection and USB mode |
| `GET /api/stream` | Local live-capture status |
| `GET /api/transcript` | Local transcription status and accepted lines |
| `GET /api/clips` | Local storage-mode clip list |
| `GET /api/analytics/<id>` | Cached local per-video analysis |

### Public relay

| Route | Purpose |
|---|---|
| `GET /healthz` | Relay and archive health |
| `GET /api/worker/{worker}` | Current public worker live state |
| `GET /api/worker/{worker}/archives` | Durable archive session list |
| `GET /api/worker/{worker}/analytics` | Public archive summary analytics |
| `GET /api/archive/{session}` | Saved session detail and transcript |
| `GET /api/archive/{session}/analytics` | Per-session public analytics |
| `GET /archive/{session}/video.mp4` | Completed public MP4 recording |

The local server binds to `127.0.0.1` by default. Pass a different port as the
first argument if necessary.
