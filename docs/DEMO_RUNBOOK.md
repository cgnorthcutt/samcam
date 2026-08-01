# Egocentric Camera Lab: operating notes

This is a compact checklist for a personal camera-capture experiment.

## Start

1. Connect a compatible camera and open the local app:

   ```bash
   python3 server.py 8011
   ```

2. In another terminal, configure a relay you control and start the publisher:

   ```bash
   EGOCAPTURE_RELAY_URL=https://<your-relay-host> \
   EGOCAPTURE_WORKER="camera-lab" \
   .venv/bin/python publish_worker.py
   ```

3. For cameras that begin in USB storage mode, press their Record button once
   and wait for macOS to re-enumerate the UVC video and audio devices.
4. Verify a current image appears at `http://localhost:8011` before checking a
   remote viewer.

## During capture

- A remote viewer needs a user click before browser audio can play.
- Keep speakers away from the microphone or use headphones. Software can limit
  feedback but cannot undo feedback that the microphone already captured.
- Treat live transcription as an optional accessibility aid, not a verbatim
  record.

## Ending and saving

1. Stop recording or disconnect the camera.
2. Leave the publisher running until its archive upload has completed.
3. Parts may be playable before the background stitch produces the final MP4.
4. The final archive uses the original camera soundtrack.

## Fast checks

```bash
curl -fsS http://127.0.0.1:8011/api/stream
curl -fsS https://<your-relay-host>/healthz
```

If the local endpoint is live but the relay is not, let the publisher retry its
outbound connection before restarting anything. If the camera has just changed
USB mode, wait for macOS to finish discovering it.
