# Sam Cam final-demo robustness checklist

This is a short preflight for the public demo. It requires no database reset,
no deletion, and no manual stream restart. Do not run it while someone is
watching a critical live feed.

## Before recording

1. Confirm the local capture server and publisher are running in separate
   terminals:

   ```bash
   curl -fsS http://127.0.0.1:8011/api/stream
   curl -fsS https://samcam.app/healthz
   curl -fsS https://samcam.app/api/worker/Curtis
   ```

   The local response is healthy when `running` is `true`. Once the camera is
   in UVC mode, it should report `live:true` with a fresh frame count. The
   public health response must be `{"status":"ok","archive":"ready"}`.

2. Connect the powered-off body camera by USB, then short-press its Record
   button. It leaves storage mode and re-enumerates as `GENERAL - UVC` plus
   `GENERAL - AUDIO`. Wait for the local `/api/stream` response to show
   `live:true`; the publisher will then update the public worker automatically.

3. Open [samcam.app](https://samcam.app) in a fresh tab. The Live view must
   show `Curtis is live` and a current picture. Click **Enable sound** once;
   browser autoplay rules require that explicit action. The feedback guard is
   intentionally conservative (band-pass + limiter + low gain), so use
   headphones or keep the laptop speaker volume low if the camera microphone
   is near the speakers.

4. Say one distinct test phrase, for example: “Sam Cam final demo check.”
   Within a few seconds the live transcript should add one line. It should not
   replay text from an earlier session or show repeated-word/stutter output.

5. Press the camera button again to return it to storage/offline state. Within
   a few seconds the public page should clear the frozen frame and say
   `Worker Curtis is not streaming at this time`. This is the expected state;
   it never substitutes archive footage for the live feed.

6. In Archive, wait for the finished session. Parts can play during encoding;
   the browser replaces them with the stitched MP4 without blocking playback.
   Verify the final recording has an enabled volume icon and its transcript.

7. Open Analytics and choose the same saved video. The chart may use planning
   estimates, but its saved-video selector must populate and the page must not
   say it is temporarily unavailable.

## If a check fails

- **Live says offline while the camera is recording:** first check local
  `/api/stream`. If `live:false`, wait for USB re-enumeration or toggle the
  physical Record button once. If local is live but public is offline, leave
  both local processes running; the publisher retries its outbound WebSocket
  automatically. Reload the public page once after the worker endpoint reports
  `streaming:true`.

- **A frame freezes after camera shutdown:** wait for the current freshness
  interval, then reload. The public UI deliberately needs a few missed status
  polls before declaring a stream offline so a momentary relay reconnect does
  not flicker the feed.

- **Archive or Analytics says reconnecting:** check `/healthz`. Do not delete
  sessions or restart the publisher. `archive:"reconnecting"` means Postgres
  is temporarily unavailable; live viewing remains independent and the browser
  preserves its last known archive list while retrying.

- **No browser sound:** click **Enable sound** in that tab. If the button is
  already set to **Mute sound**, toggle it off and on once. Browser autoplay
  cannot be bypassed by the app.

- **Archive video is gray or cannot load:** leave the Archive tab open. It
  retries temporary media failures and falls back to playable parts while the
  stitched MP4 is being served.

## Built-in guards

- Render uses one web instance because current live-frame state is intentionally
  in memory; scaling that process horizontally would split viewers from the
  publishing worker.
- Archive media is written directly to Postgres; the relay does not retain full
  uploaded MP4 data in memory during normal database operation.
- Archive deletion creates a durable tombstone, preventing an offline
  publisher’s local recovery copy from restoring a deleted session.
- Local audio is saved alongside each archive part and muxed to AAC. Finished
  sessions preserve that original camera track in the final archive MP4; there
  is no automatic offline audio substitution in public playback.
- The transcription pipeline rejects repeated phrases, character stutters, and
  implausibly long text before it reaches live or archived views.

## Fast regression suite

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The suite uses local fixtures and in-memory archive contracts. It does not need
the camera, a network connection, Render credentials, or a running server.
