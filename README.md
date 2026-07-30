# Sam Cam

Sam Cam is a local live-view and transcription app for the
[Mini Body Camera B08KY7KLPB](https://www.amazon.com/dp/B08KY7KLPB). It only
uses the body camera's USB video and audio devices; it never substitutes the
Mac camera, an iPhone, or another webcam.

## Run

Install FFmpeg:

```bash
brew install ffmpeg
```

Build the native camera helper:

```bash
swiftc -O bodycam_capture.swift -o bodycam_capture \
  -framework AVFoundation -framework CoreImage -framework CoreMedia \
  -framework CoreVideo -framework AudioToolbox
```

Install the optional free, fully local transcription model:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-transcription.txt
```

Start the app on its usual demo port:

```bash
python3 server.py 8011
```

Open [http://localhost:8011](http://localhost:8011). The first transcription
run downloads `mlx-community/whisper-large-v3-turbo`; later runs use the local
model cache.

## Camera flow

The camera has two mutually exclusive USB modes:

1. Connect the powered-off camera over USB.
2. Sam Cam reports **storage mode** and makes recorded clips available in
   Archive.
3. Short-press the camera's Record/ON-OFF button while it remains connected.
4. The device re-enumerates as `GENERAL - UVC` plus `GENERAL - AUDIO`.
5. Sam Cam detects the transition, opens Stream automatically, and starts live
   video and transcription.

When the camera is unplugged, Stream shows **Connect camera**. It never fills
the live view with archived footage. When the camera returns to storage mode,
Stream shows **Press Record to go live**.

The native helper owns video and audio in one `AVCaptureSession`. It emits JPEG
frames and 16 kHz mono PCM to the server. Audio is buffered in a small bounded
queue so transcription cannot stall video capture.

## Behavior

- **Stream** is live USB UVC only. The browser receives one shared MJPEG feed
  from `/stream.mjpg`.
- **Live transcript** uses the body camera microphone and local MLX Whisper.
  Nothing is sent to a paid transcription API.
- **Archive** shows recordings discovered in the camera's video folder.
  Browser-incompatible camera files are cached as H.264 MP4 plus thumbnails in
  `.cache/`.
- **Customer market fit** analyzes each ready video. FFmpeg samples real
  luminance and frame-to-frame motion across the clip, then combines those
  measurements with adjustable battery and mounting assumptions to show
  battery ETA, static mount-moment estimates, a wearability proxy,
  customer-segment suitability, and an operating-state comfort-versus-capture
  Pareto frontier.
- **Clock sync** happens automatically once each time the camera mounts in
  storage mode.
- The live UVC feed is not saved by this app. Any on-camera recording behavior
  remains controlled by the camera firmware.

Transcription can be tuned with environment variables:

```bash
BODYCAM_WHISPER_MODEL=mlx-community/whisper-large-v3-turbo \
BODYCAM_TRANSCRIPT_LANGUAGE=en \
BODYCAM_TRANSCRIPT_CHUNK_SECONDS=3 \
BODYCAM_TRANSCRIPT_MIN_AVG_LOGPROB=-1.0 \
python3 server.py 8011
```

`BODYCAM_TRANSCRIPT_MIN_AVG_LOGPROB` rejects uncertain model output from
camera noise. Lower it to make transcription more permissive; raise it to
favor fewer false lines.

## Useful files

```text
server.py             HTTP server, USB discovery, capture and archive
bodycam_capture.swift native body-camera video/audio capture
transcribe_worker.py  persistent local MLX Whisper worker
static/index.html     single-page interface
probe.sh              USB mode watcher
tail_probe.py         storage-file growth diagnostic
ideas.md              investigation history and fallback ideas
```

## HTTP endpoints

| Route | Purpose |
|---|---|
| `GET /` | Sam Cam interface |
| `GET /stream.mjpg` | live MJPEG body-camera feed |
| `GET /api/status` | camera connection and USB mode |
| `GET /api/stream` | live-capture status |
| `GET /api/transcript` | local model status and transcript lines |
| `GET /api/clips` | archive clip list |
| `GET /api/analytics/<id>` | cached per-video motion, lighting, and product-fit inputs |
| `GET /media/<id>.mp4` | cached browser-ready clip |
| `GET /thumb/<id>.jpg` | clip thumbnail |
| `GET /raw/<id>` | original camera file |

The server binds to `127.0.0.1` only. Pass a different port as the first
argument if needed.

Analytics separates video measurements from product assumptions in the UI.
Battery and ergonomic values are planning estimates—not battery telemetry,
market research, medical advice, or direct biomechanical measurements. The default product
model uses the listing specifications for an 800 mAh battery, 4.5-hour nominal
runtime, and 3.17 oz / 89.9 g device weight.

## Public live demo

The camera itself remains connected to a worker's laptop. To make that live
view available on the internet, this repository now includes a tiny relay that
runs on Render and a local publisher that makes one **outbound** WebSocket
connection to it. No laptop port forwarding or public IP is needed.

```text
USB body camera → local Sam Cam (server.py) → publish_worker.py
                                             ↓ outbound WSS
                                   Render relay → public web viewers
```

The Render relay holds only the current JPEG frame and recent transcript lines
in memory. It does not pull from Archive and will show exactly **“Worker Curtis
is not streaming at this time”** when the publisher, live camera, or its current
frame is unavailable. The implementation deliberately uses one Render instance:
the live state is in memory and must not be load-balanced between instances.

### Run a worker

Start the regular local app, then the publisher in another terminal:

```bash
python3 server.py 8011

uv pip install --python .venv/bin/python -r requirements-publisher.txt
SAMCAM_RELAY_URL=https://samcam.app \
SAMCAM_WORKER=Curtis \
.venv/bin/python publish_worker.py
```

The publisher only forwards `GET /stream.mjpg`, `GET /api/stream`, and
`GET /api/transcript` from this local body-camera app. When the camera is
unplugged or changes back to storage mode, it reports the worker offline and
the public page clears the frame. A different laptop can publish under a
different worker name by changing `SAMCAM_WORKER`; viewers use
`https://samcam.app/?worker=Name`.

### Deploy the relay on Render

`render.yaml` defines the service as a Python web service with a `/healthz`
check. Import the repository as a Render Blueprint (or create a Python web
service with the same commands):

```text
Build command: pip install -r requirements-relay.txt
Start command: uvicorn cloud.main:app --host 0.0.0.0 --port $PORT
```

The archive requires a Render Postgres instance. In the Sam Cam service's
**Environment** settings, set `DATABASE_URL` to the Postgres service's
**Internal Database URL** (the service and database must be in the same Render
region). Do not paste a hostname by hand. On boot, `/healthz` must report
`{"status":"ok","archive":"ready"}`. `archive:"reconnecting"` means the
relay is keeping live video available while it retries Postgres, but it will
intentionally return 503 for Archive rather than pretending the durable
archive is empty. The relay replays any session data received during a brief
database outage once the connection returns.

Use the Render service's generated `*.onrender.com` URL as `SAMCAM_RELAY_URL`
until the custom domain is active. Then add `samcam.app` in Render's Custom
Domains screen. For Namecheap, Render's documented root-domain setup is an
`A` record for `@` pointing to `216.24.57.1`; wait for Render to verify the DNS
record and provision TLS. The `www` alias can be a CNAME to the Render service
hostname if wanted. See Render's [custom-domain guide](https://render.com/docs/custom-domains)
and its [Namecheap DNS guide](https://render.com/docs/configure-namecheap-dns).

### Demo safety

This relay is intentionally public and unauthenticated, as requested for the
demo. Anyone who knows a worker name can watch it, and anyone can attempt to
publish under a worker name. Delete the Render service after the demo, and do
not use this configuration for private video. Never commit a Render API key or
any other credential to this repository.
