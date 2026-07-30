# Sam Cam final-demo runbook

This runbook is deliberately short. Follow it in order; avoid restarting a
working local camera process just because the public Archive is reconnecting.

## Before the audience arrives

1. Connect the body camera by USB and make sure it has power.
2. In terminal one, start the local app:

   ```bash
   python3 server.py 8011
   ```

3. In terminal two, start the public publisher:

   ```bash
   SAMCAM_RELAY_URL=https://samcam.app \
   SAMCAM_WORKER=Curtis \
   .venv/bin/python publish_worker.py
   ```

4. Open both [local Sam Cam](http://localhost:8011) and
   [samcam.app](https://samcam.app/?worker=Curtis).
5. Check the public service before recording:

   ```bash
   curl -fsS https://samcam.app/healthz
   ```

   Continue only when the reply reports `"status":"ok"` and
   `"archive":"ready"`. If archive is reconnecting, live can still be
   demonstrated, but new public Archive/Analytics results may be delayed.

## Start a live demo

1. With the camera connected, short-press its Record/ON-OFF button.
2. Wait while macOS changes it from storage mode to `GENERAL - UVC` and
   `GENERAL - AUDIO`. This can take several seconds.
3. Confirm the local page shows a current camera image.
4. Confirm the public page changes to **Curtis is live** and shows the same
   current scene. The public page polls automatically; one browser reload is
   reasonable if it stays in a connecting state.
5. If demonstrating sound, click **Enable sound** in the public page. Browsers
   require that click. Prefer headphones and keep speakers away from the camera
   microphone; the built-in filtered/compressed playback reduces feedback but
   cannot eliminate it in every room.
6. Speak a short, clear sentence near the camera. Treat the resulting text as
   an assistive live transcript, not an exact quote.

## End a live session and demonstrate Archive

1. Press the camera button to stop/leave live mode, or disconnect it.
2. Wait for the local publisher to finalize the session. A completed recording
   is archived in the background; do not stop the publisher or power off the
   worker laptop until the upload completes.
3. Open **Archive** at samcam.app. A running or recently ended session can show
   playable parts first; finalized sessions become one MP4 automatically.
4. Open **Analytics** only after a completed session appears. It is calculated
   from the saved video and may arrive after the recording.
5. The two tagged demonstration clips should say **Captured with Meta Oakley
   Vanguard AI Glasses** during playback. Their visible dates are original
   QuickTime capture dates.

## Fast status checks

Run these from the worker laptop when something looks wrong:

```bash
curl -fsS http://127.0.0.1:8011/api/stream
curl -fsS https://samcam.app/api/worker/Curtis
curl -fsS https://samcam.app/healthz
```

Interpretation:

| Local `/api/stream` | Public worker endpoint | What to do |
|---|---|---|
| `live: true` | `streaming: true` | The relay is healthy; refresh a stale viewer page once if necessary. |
| `live: true` | `streaming: false` | The local publisher is not connected or uses a different worker name. Check terminal two. |
| `live: false` | `streaming: false` | Camera is off, unplugged, in storage mode, or still re-enumerating. Press Record and wait. |
| Any | Any, with health `archive: reconnecting` | Live may work; durable Archive/Analytics are recovering. Check Render/Postgres, not the camera first. |

## Recovery checklist

### Local camera is not live

1. Verify the camera is plugged in and has power.
2. Short-press Record/ON-OFF once to request UVC mode.
3. Wait for macOS to re-enumerate it. Do not rapidly toggle the button.
4. If local `/api/stream` remains `live: false`, stop only the local app and
   start `python3 server.py 8011` again. Then leave the publisher running; it
   reconnects automatically.

### Local works, public page says Curtis is offline

1. Confirm local `/api/stream` reports `live: true`.
2. Check that terminal two is still running and has
   `SAMCAM_WORKER=Curtis` and `SAMCAM_RELAY_URL=https://samcam.app`.
3. Restart only the publisher if it is not connected:

   ```bash
   SAMCAM_RELAY_URL=https://samcam.app \
   SAMCAM_WORKER=Curtis \
   .venv/bin/python publish_worker.py
   ```

4. Reload the public page once after the worker endpoint reports
   `streaming: true`.

### Sound or transcript is missing

- Sound requires a viewer click on **Enable sound**. If it is already enabled,
  turn it off and on once after a browser reload.
- Do not judge the transcript from silence, feedback tones, distant speech, or
  the first seconds after a camera mode change. Clear speech and a short pause
  give the local model the best chance to produce an accepted line.
- The transcript guard deliberately rejects low-confidence or repetitive
  output. “No recognized speech yet” is a valid result.

### Public Archive or Analytics is unavailable

1. Check `https://samcam.app/healthz`.
2. If it reports `archive: reconnecting` or the site gives HTTP 503, check the
   Render web service and its Postgres capacity/health.
3. Keep the local app and publisher running while the relay recovers, so the
   active session can continue locally. Do not mistake a temporary archive
   outage for successful persistence.
4. When health returns to `archive: ready`, let the publisher finish syncing
   before ending the laptop session.

## Public archive deletion

First list and double-check the target session ID in Archive. Then run:

```bash
.venv/bin/python purge_archives.py --dry-run SESSION_ID
.venv/bin/python purge_archives.py SESSION_ID
```

This permanently removes the named public session and prevents connected
publishers from re-uploading it. It leaves the local `archives/` copy alone.

## After the demo

Stop recording, keep the publisher alive until the final Archive and Analytics
entries are visible, then stop both local processes. This deployment is public
and unauthenticated; remove the Render service when the demo is no longer
needed.
