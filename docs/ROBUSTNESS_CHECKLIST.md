# Egocentric Camera Lab: quick validation

Run these checks before recording a camera experiment:

1. Start the local capture server and publisher in separate terminals.
2. Confirm `http://127.0.0.1:8011/api/stream` responds.
3. Put the camera into UVC/live mode and wait for a fresh local frame.
4. Confirm the relay’s `/healthz` endpoint reports the archive is ready.
5. Open a remote viewer, verify a current image, then explicitly enable audio
   if it is needed.
6. Say one short phrase and check that the optional transcript contains a
   plausible line rather than stale text.
7. Stop the camera and verify the live image clears rather than freezing.
8. Keep the publisher running until the archive exposes playable parts or the
   final MP4.
9. Open the capture-profile page only after the selected recording is saved.

The quick offline suite is intentionally camera- and network-free:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

It validates relay message contracts, archive fallbacks, original-audio
preservation, and browser-view data shape with small local fixtures.
