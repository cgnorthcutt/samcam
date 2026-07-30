# Live streaming from the body cam — idea log

Goal: a genuine live (or near-live) stream **from the body cam device itself**.
Not the phone, not the Mac camera. Minimum bar: record while plugged in and see
it appear on screen immediately.

## Hardware facts (established, do not re-test)

- USB ID `1b3f:8301`, product string `eEGENARPLUL-SSMC` (Generalplus).
- Enumerates with `bNumConfigurations = 1`, one interface, `bInterfaceClass = 8`
  (mass storage), `bInterfaceSubClass = 6` (SCSI), `bInterfaceProtocol = 80`
  (bulk-only transport). USB 2.0 (`bcdUSB = 0x0200`).
- Records MJPEG in AVI, 1920x1080, ~30fps, to a 124.6 GB FAT32 volume.
- Recording **stops the instant USB data connects**. Verified both plug orders,
  through a full unmount/remount, and confirmed at the device.
- No Wi-Fi, no Bluetooth.
- Manufacturer manual (KSAD BOSSBO) claims: *"It is possible to use the camera as
  a webcam, but not all programs support this camera."* Recommends NCH Debut.
- Manual also: *"When connected to an external charger, camera will automatically
  power on and starting recording."* — power-only ≠ data connection.
- Generalplus siblings expose distinct PIDs per personality:
  `1b3f:0c52` = 808 Camera mass storage, `1b3f:2002` = 808 Camera **web-cam mode**.

## Status key

`UNTRIED` · `IN PROGRESS` · `WORKS` · `DEAD END (with reason)`

---

## Tier 1 — software-only mode switch (no user action)

### I1. USB mode-switch via vendor control transfer — UNTRIED
Dual-personality USB devices commonly flip modes on a vendor-specific control
transfer (the `usb_modeswitch` pattern). If Generalplus has one, this turns the
camera into a UVC webcam entirely in software.
Attack: claim the device with libusb after unmounting, enumerate descriptors,
sweep plausible vendor requests.

### I2. Mode switch via SCSI vendor command — UNTRIED
Subclass 6 / protocol 80 means SCSI over bulk-only transport. Many cameras switch
personality on a vendor CDB (opcode `0xFF`, `0xCF`, etc.) sent to the block
device. On macOS reaching raw SCSI needs the volume unmounted and either
`libusb` bulk transfers implementing BOT by hand, or IOKit SCSI passthrough.
Note: we can implement bulk-only transport ourselves over libusb — this is very
achievable and does not need any kext.

### I3. Probe for hidden configurations / alt settings — UNTRIED
`bNumConfigurations = 1` in *this* personality, but alternate interface settings
may still hide a video-class alt. Enumerate every interface's alt settings via
libusb.

### I4. GoPlus Cam / vendor app protocol — UNTRIED
Generalplus ships a "GoPlus Cam Plug-in" Android app. If a vendor protocol
exists, its handshake may reveal the mode-switch command. Research target.

---

## Tier 2 — exploit the recording grace window

### I5. Rapid data-line cycling — UNTRIED
The camera keeps recording for a few seconds after data connects, then stops.
If USB *data* is disconnected and reconnected on a cycle, each reconnect could
yield a few seconds of fresh footage. Needs per-port control (`uhubctl`) or a
data-switchable hub. Gives chunked near-live at maybe 10–20s lag.

### I6. Power-only tether + periodic data connect — UNTRIED
Manual says it records when on an external *charger*. A cable with data lines
switched (or a hub with per-port data control) would let it record continuously
on power, and we connect data briefly to siphon clips.

---

## Tier 3 — physical / mode discovery (needs the user, but cheap)

### I7. Power on FIRST, then plug in — UNTRIED, HIGH PRIORITY
The manual's own webcam claim most likely requires this order. Every test so far
plugged in while the camera was off. `probe.sh` detects the moment the bus
personality changes (class 14 = UVC).

### I8. Button combos during connect — UNTRIED
Hold power while inserting; tap power once connected; double-tap (night vision
toggle) while connected.

---

## Dead ends

### D1. Recording while attached over a normal data cable — DEAD END
Firmware disables it. Tested exhaustively, confirmed by the manual. Any solution
must avoid a plain data connection during capture.

### D2. Live feed from the mass-storage personality — DEAD END
Class 8 exposes blocks, not video. No amount of host software makes it a camera
while it enumerates this way.

---

## 2026-07-28 — Codex research sidecar

### R1. Correct mode-switch sequence for this ASIN — READY TO TEST, HIGHEST PRIORITY

The exact Amazon-hosted manual for ASIN `B08KY7KLPB` gives a two-stage sequence:

1. Start with the camera off.
2. Connect its data cable to the computer and wait for the first device to
   install/appear. This is expected to be `1b3f:8301` / `GENERALPLUS-MSDC`.
3. **Short-press ON/OFF once while it remains connected.**
4. Wait for a second driver/device to appear, then select `GENERAL-UVC`.

This corrects I7: do **not** power the camera on before connecting as the first
test. An OEM Generalplus/Joyhonest camera-board brief describes the state
transition even more explicitly: USB connection enters `Genplus USB-MSDC Disk`
automatically, and pressing ON/OFF while connected enters `GENERAL-UVC` (and on
that board, `GENERAL-AUDIO`). The switch is therefore expected to look like a
USB disconnect/re-enumeration, not an alternate setting appearing inside PID
`8301`.

Practical test: run `probe.sh`, attach the off camera, positively observe class
8/PID `8301`, then give ON/OFF a quick tap (roughly 0.1–0.5 s; do not hold it
long enough to invoke normal power-off). Allow up to 15 seconds for `8301` to
disappear and a class-14 interface to arrive. Repeat once on a direct Mac port
or simple USB 2 hub if a USB-C dock suppresses the reconnect.

### R2. Do not hard-code PID `2002` as the only success state

`1b3f:2002` is registered as “808 Camera #9 (web-cam mode),” and the USB ID
repository contains a concrete report of a camera that uses `8301` in mass
storage and `2002` in webcam mode. That makes `2002` the leading expectation,
but neither the ASIN manual nor the OEM brief states a PID. `1b3f:2202` and
`1b3f:2247` are also deployed as `GENERAL-UVC`/`GENERAL WEBCAM` personalities.
No source found ties PID `2247` specifically to this ASIN.

Action for detection: identify success as vendor `0x1b3f` **plus an interface
with `bInterfaceClass == 14`**, accepting at least PIDs `2002`, `2202`, and
`2247`; report the observed PID rather than requiring `2002`. Do not use PID
alone: Generalplus reuses IDs across firmware/products, and a UVC composite
device may have device-level class `0xef` (239) while only its interfaces carry
class 14.

### R3. `2002`/`2247` are deliberately tolerated, non-compliant UVC 1.0 devices

Linux upstream documented a real `1b3f:2002` descriptor defect: its Processing
Unit and Output Terminal both use entity ID 5. A strict parser stopped creating
the video node, so Linux commit `8004d635f27b` reverted that strictness to
restore this camera. Reports for `1b3f:2247` show the same duplicate ID 5, an
over-short Processing Unit descriptor, unsupported `GET_DEF(PROBE)`, and an
invalid interrupt endpoint `bInterval` of 32. These are device defects, not
evidence that the webcam mode failed to engage.

Concrete consequence on macOS: separate these readiness gates in logs:

1. `8301` disappears.
2. A `1b3f:*` USB device with a class-14 interface appears.
3. AVFoundation publishes a capture device.
4. The app receives and decodes its first non-empty frame.

Only gate 4 means “live.” There is an empirical macOS report where
`1b3f:2002` appeared in System Information and in Photo Booth as
`GENERAL - UVC`, yet produced a black image. If gate 2 succeeds but gate 3
doesn't, suspect macOS rejecting the malformed UVC graph. If gate 3 succeeds
but gate 4 doesn't, retry capture negotiation rather than falling back to disk
playback silently.

### R4. Reliable macOS correlation and capture strategy

- At the USB layer, retain `idVendor`, `idProduct`, every interface class, and
  `locationID`. Correlate the departing `8301` and arriving UVC personality by
  the same physical USB location. Poll faster than the present two-second cache
  during the button-switch window so the transient detach is visible.
- At the media layer, use an `AVCaptureDevice.DiscoverySession` including
  `.externalUnknown`, and observe its device list or connected/disconnected
  notifications. Apple documents `uniqueID` as the persistent capture-device
  identifier. Keep `localizedName`, `manufacturer`, `modelID`, and `uniqueID`;
  names such as `GENERAL-UVC` are not unique.
- The current “first camera whose name does not look built-in” heuristic can
  select an unrelated USB camera. Require the Generalplus USB transition first,
  then re-list AVFoundation devices and select the newly arrived device. Never
  keep an AVFoundation numeric index across reconnects; FFmpeg indexes are
  assigned from the current device list.
- FFmpeg's authoritative AVFoundation input syntax supports a device name or
  freshly listed index and `none` for audio. Start video-only (`INDEX:none`) so
  PID `2247`'s questionable USB-audio descriptors and microphone permission
  cannot block video. AVFoundation supplies decoded raw pixel formats to
  FFmpeg; do not force `mjpeg` as an AVFoundation input pixel format.
- Negotiate the actual formats exposed by the arriving device. As a recovery
  matrix, try default format first, then 1280x720@30, 640x480@30, and
  1920x1080@30, requiring a first frame within three seconds each. A documented
  `2247` instance exposes MJPEG at 1920x1080@30 plus lower resolutions, but
  Generalplus firmware variants differ.
- Camera permission is independent of USB enumeration. On macOS 10.14+, the app
  needs `NSCameraUsageDescription` and must check/request AVFoundation video
  authorization. A denied/not-yet-answered authorization can yield black
  frames. Microphone permission is unnecessary for the first live-video proof.

### R5. Focused experiment matrix

1. **Canonical switch:** off → connect → verify `8301` → short-press ON/OFF.
2. **Press-duration sweep:** if no detach occurs, repeat from fully disconnected
   with taps of approximately 0.1 s, 0.5 s, and 1.0 s. Stop before the normal
   long-press power-off duration.
3. **Transport isolation:** repeat canonical switch with the supplied cable and
   a direct/simple USB 2 path; charge-only cables cannot produce either
   personality, and some docks can mishandle rapid re-enumeration.
4. **USB-vs-capture diagnosis:** save one timestamped log containing all four
   gates from R3. This distinguishes “button never switched firmware” from
   “macOS saw malformed UVC but AVFoundation rejected it.”
5. **First-frame negotiation:** after every newly observed UVC PID, re-list
   AVFoundation devices, open video with audio disabled, run the format matrix,
   and checksum several frames so a repeated black/frozen buffer is not counted
   as live.

No authoritative evidence was found for a host-issued SCSI or vendor control
request that changes `8301` into UVC. The documented switch is the physical
ON/OFF key after MSDC enumeration, so test R1 before further blind request
sweeps.

### Sources

- [Amazon-hosted manual for ASIN B08KY7KLPB, pp. 13–15](https://m.media-amazon.com/images/I/A1Hpyx1gxXL.pdf)
- [Joyhonest 2503 Generalplus camera-board product brief, pp. 3–4](https://img.banggood.com/file/products/20150922033901FHD%201080P%202503%20Camera%20Manual%20from%20Wind%20Joyhonest%20%2020150710.pdf)
- [USB ID Repository entry and mode-pair reports for 1b3f:2002](https://usb-ids.gowdy.us/read/UD/1b3f/2002)
- [Linux upstream revert documenting duplicate entity ID 5 on 1b3f:2002](https://git.zx2c4.com/wireguard-linux/commit/?id=8004d635f27bbccaa5c083c50d4d5302a6ffa00e)
- [Linux-media report documenting the corresponding 1b3f:2247 descriptor faults](https://www.spinics.net/lists/linux-media/msg268550.html)
- [Canaan K230 SDK capture report and formats for 1b3f:2247](https://www.kendryte.com/k230_linux/en/main/app_develop_guide/user_develop/uvc.html)
- [Apple: capture-device discovery and connection monitoring](https://developer.apple.com/documentation/avfoundation/avcapturedevice/discoverysession)
- [Apple: persistent AVCaptureDevice uniqueID](https://developer.apple.com/documentation/avfoundation/avcapturedevice/uniqueid)
- [Apple: camera authorization requirements on macOS](https://developer.apple.com/documentation/bundleresources/requesting-authorization-for-media-capture-on-macos)
- [FFmpeg AVFoundation device selection and input options](https://www.ffmpeg.org/ffmpeg-devices.html#avfoundation)
- [Empirical macOS 1b3f:2002 enumeration-with-black-video report](https://superuser.com/questions/1538189/digital-microscopeequivalent-to-an-old-webcam-on-macos)

---

## 2026-07-28 — implementation and device-test checkpoint

- **WORKS:** the server now recognizes any `1b3f:*` interface-class-14
  personality, chooses the stable `GENERAL-UVC` AVFoundation name, and refuses
  to substitute the Mac, iPhone, screen capture, or an unrelated webcam.
- **WORKS:** UVC hot-plug preempts disk replay. A synthetic eight-second replay
  was interrupted 0.52 seconds after simulated `GENERAL-UVC` arrival.
- **WORKS:** live JPEG framing publishes on JPEG EOI instead of holding a whole
  frame until the next SOI. A 12-frame image2pipe test emitted all 12 complete
  frames.
- **WORKS:** live capture has a first-frame/stall watchdog, drains FFmpeg
  diagnostics, drops old frames if output backs up, retries with default,
  640x480, 1280x720, and 1920x1080 profiles, and reports the active profile or
  last error through `/api/stream`.
- **FIXED:** `usbtool.py` calculated BOT `bCBWCBLength` by stripping zero bytes
  from the CDB. It now sends the caller's actual 1–16 byte command length.
- **BLOCKED ON THIS MAC:** after a clean volume unmount, libusb still cannot
  claim interface 0 (`Access denied`), so no further SCSI experiment was run.
  The volume was explicitly remounted and all footage remains visible.
- **DO NOT RUN:** blind vendor-opcode probing remains unsafe and is not evidence
  for a mode switch. The canonical physical short-press sequence is both safer
  and directly documented.
- **CURRENT DEVICE STATE:** `1b3f:8301`, class 8 mass storage. The missing event
  is the documented short ON/OFF tap that makes the camera re-enumerate as
  `GENERAL-UVC`; software is armed to capture it as soon as it appears.

---

## 2026-07-28 — LIVE STREAM ACHIEVED

### Verified operating sequence — WORKS

1. Start the server: `/opt/homebrew/bin/python3 server.py 8011`.
2. Connect the powered-off camera over its USB data cable.
3. Wait for `1b3f:8301` / mass-storage mode.
4. Single-tap the camera's ON/OFF/Record button while it remains connected.
5. The storage volume disappears and the same physical USB location
   re-enumerates as `1b3f:2002`, product `GENERAL - UVC`.
6. Open `http://localhost:8011`; `/stream.mjpg` switches to the live camera.

The button the user calls Record is the correct button. It is the same physical
ON/OFF control described in the manual. Do not long-hold (power off) or
double-tap (IR toggle).

### Hardware/capture facts measured from this exact unit

- UVC USB ID: `1b3f:2002`; product `GENERAL - UVC`; same `locationID` as the
  mass-storage personality.
- AVFoundation video name: `GENERAL - UVC`.
- AVFoundation audio name: `GENERAL - AUDIO` (not needed for the first stream).
- The only accepted video mode is **1280x720 at exactly 30 fps**.
- AVFoundation exposes `uyvy422`, `yuyv422`, `nv12`, `0rgb`, and `bgr0`; it
  automatically selected `uyvy422` when no pixel format was forced.
- Opening by stable name works:
  `-f avfoundation -framerate 30 -video_size 1280x720 -i "GENERAL - UVC:none"`.

### Proof that it is genuinely live — WORKS

- A five-second `framemd5` capture from the exact `GENERAL - UVC` device
  produced changing hashes on every emitted frame.
- The app's `/stream.mjpg` delivered 10,198,208 bytes in 12 seconds.
- Parsing that response found **225 complete JPEG frames and 225 unique MD5
  hashes**; the first and last hashes differed.
- During the run `/api/stream` reported:
  `source="LIVE · GENERAL - UVC"`, `live=true`,
  `live_profile="1280x720@30"`, and `live_error=null`.
- The in-app browser rendered the MJPEG element at **1280x720**, visibly
  unhidden, and identified the source as `LIVE · GENERAL - UVC`.

### Critical implementation fix

In UVC mode the macOS driver claims the interfaces and the old
`IOUSBHostInterface` query no longer exposes the `"USB Device Info"` property.
That made the first detector miss a camera that AVFoundation could already use.
Live readiness now comes from the unique AVFoundation name containing both
`GENERAL` and `UVC`; the app opens that stable name, never a Mac/iPhone/screen
device and never a reconnect-sensitive numeric index. The known-good
`1280x720@30` profile is attempted immediately after the default probe.

---

## 2026-07-28 — minimal live-only sidecar

- Added `live_only.py`, independent of `server.py`, the clip archive, and the UI.
- It has four explicit gates: same-port Generalplus class-14 USB personality;
  unambiguous AVFoundation arrival; multiple changing decoded `framemd5`
  hashes; and live MJPEG publication.
- It refuses MacBook/FaceTime, Continuity iPhone/iPad, screen capture, OBS,
  saved clips, and unrelated webcams. There is no fallback source.
- It selects a unique AVFoundation name because names survive index changes.
  A numeric index is used only for duplicate names and is re-listed immediately
  before each open.
- Format negotiation tries the device default, then common 640x480, 1280x720,
  and 1920x1080 profiles at 30 fps, plus lower-rate recovery profiles.
- `GET /stream.mjpg` is the direct multipart MJPEG endpoint and
  `GET /status.json` reports which gate is blocking.
- If USB class 14 exists but AVFoundation has no matching arrival, the likely
  causes are malformed Generalplus UVC descriptors or macOS camera
  authorization. Explicit FFmpeg permission errors produce the exact System
  Settings recovery instruction.
- Pressing Record in `8301` storage mode is not a live-stream route: prior
  hardware tests showed recording stops as USB storage takes over. The useful
  physical action is the documented short ON/OFF press after `8301` appears.

---

## 2026-07-28 — bodycam audio and local live transcription

### Verified

- UVC mode exposes `GENERAL - AUDIO` as a real AVFoundation microphone.
- A direct six-second capture produced mono speech audio (peak about -19.5 dB).
- Free local `mlx-community/whisper-large-v3-turbo` correctly transcribed that
  bodycam sample in about 2.4 seconds on the M4 MacBook Air.
- The model and `mlx-whisper` runtime are installed in the project's `.venv`;
  no cloud API, account, key, or per-minute charge is involved.

### Critical firmware constraint and solution

- Two independent AVFoundation processes cannot own `GENERAL - UVC` and
  `GENERAL - AUDIO` concurrently on this firmware; the video endpoint stalls.
- Use exactly one FFmpeg input, `GENERAL - UVC:GENERAL - AUDIO`, and split it
  into MJPEG video plus 16 kHz mono PCM outputs inside that process.
- Feed PCM to a persistent MLX Whisper worker over a pipe. The worker drains
  that pipe on a reader thread while inference runs so model latency cannot
  backpressure and freeze live video.
- Starting/stopping transcription restarts the single capture process with or
  without its audio output; it must never open a second camera process.
- A force-killed experimental dual-open can leave macOS's privileged UVC
  helper wedged. A short physical camera-mode toggle re-enumerates the device
  and is the reliable recovery mechanism available without administrator
  privileges.

### Native single-session implementation

- Added `bodycam_capture.swift`, based on the proven `AVCaptureSession` pattern
  in `/Users/cgn/Downloads/transcribing_app/agent/capture.swift`.
- It binds the exact stable bodycam devices only, places both inputs in one
  session, emits length-prefixed JPEG `V` packets and 16 kHz mono s16le `A`
  packets, and never selects a MacBook or iPhone source.
- `server.py` now prefers this helper, publishes its video packets directly,
  and pipes audio packets to the persistent MLX Whisper worker.
- The complete HTTP PCM route was verified with a real bodycam microphone
  sample and emitted transcript events at about 1–2 seconds inference time.
- The browser-native experiment also delivered bodycam PCM to Whisper, proving
  live mic acquisition, but Codex's embedded browser exposed a 2x2 black video
  placeholder. Keep native capture primary and do not auto-activate the browser
  fallback.
- Current hardware state after the earlier dual-open test enumerates both exact
  devices but emits zero video and zero audio sample buffers even to a fresh
  native session. The code and an independent probe agree that a physical
  unplug/full-off/reconnect/storage-then-short-tap cycle is required.

---

## 2026-07-28 — reliability cleanup after the successful live demo

- The Stream view is now strictly gated on `live=true` plus a fresh UVC frame.
  A stale archive frame can never satisfy that condition.
- Native audio delivery now crosses a bounded, drop-oldest queue before writing
  to Whisper. Slow inference or a full worker pipe therefore cannot block the
  shared packet reader and freeze bodycam video.
- USB storage discovery now requires the known Generalplus vendor/product
  identity before scanning a mounted video folder or automatically writing
  `time.txt`; unrelated USB drives are ignored.
- Cached camera clips remain available in Archive while the camera temporarily
  unmounts to enter UVC mode.
- Capture, transcription, HTTP, scanning, and transcode resources now have an
  explicit shutdown path.
- Removed the old file-replay session controls, browser-camera fallback, manual
  rescan/clock/transcription routes, and other paths that no longer belong to
  the live-only product.

---

## 2026-07-29 — per-video customer market fit analytics

- Replace the Demo strip with a dashboard keyed to every ready video.
- Measure what the pixels can support: sampled luminance, lighting quality,
  frame-to-frame motion, stability, and capture utility across clip time.
- Keep product assumptions explicit and adjustable. The default model uses the
  listing's 800 mAh battery, 4.5-hour runtime, and 3.17 oz / 89.9 g weight;
  starting charge, mount distance, and target session can be changed live.
- Estimate battery drain/ETA, static mount moment, wearability, and
  customer-segment fit without presenting those values as hardware telemetry
  or medical measurements.
- Plot each video sample in comfort × capture-utility space and identify the
  non-dominated Pareto frontier, so improvements in one dimension are visible
  alongside tradeoffs in the other.
- Cache analysis by clip fingerprint and cap FFmpeg work at about 180 tiny
  grayscale frames so long videos remain inexpensive to inspect.

---

## 2026-07-29 — transcript observability and recording verification

- Verified the saved `GENERAL - AUDIO` sample through the exact local
  large-v3-turbo worker; it emitted transcript events successfully.
- Reduced the default live speech window from five seconds to three so a short
  UVC capture interval can still produce text.
- Added received, written, dropped, elapsed-audio, silence-event, and RMS
  diagnostics to `/api/transcript`; the UI now distinguishes no camera audio,
  audio awaiting a full chunk, detected silence, and recognized speech.
- Confirmed the current UVC path publishes JPEG frames in memory only. The app
  saves transcoded Archive copies and analytics caches, but it does not yet
  write the live feed to a recording file.
- Root-caused live digital silence in the native helper: it queried an
  `AVAudioPCMBuffer` destination size while `frameLength` was still zero, so it
  copied zero bytes from every valid camera microphone sample. Setting
  `frameLength` before copying now produces simultaneous live video and nonzero
  audio (verified at 130 frames plus 4.8 seconds of PCM in one helper session).
- Whisper repeatedly hallucinated “Thank you” on low-information background
  chunks. Filter known low-confidence silence phrases and repeated
  low-confidence duplicates while preserving changing speech.

---

## 2026-07-29 — public Render relay

- **WORKS (local end-to-end):** an isolated FastAPI relay accepts an outbound
  WebSocket publisher from a laptop, publishes current JPEG frames as MJPEG,
  relays transcript lines, and clears the frame immediately when the publisher
  disconnects.
- The public service has no path to Archive and stores only the newest frame
  plus a short in-memory transcript buffer. A worker is online only when it
  reports live status and continues sending fresh frames; otherwise viewers see
  `Worker Curtis is not streaming at this time`.
- `publish_worker.py` consumes the existing local `server.py` MJPEG/status/
  transcript endpoints. It keeps camera capture local and uses an outbound WSS
  connection, so a worker laptop needs neither a public IP nor port forwarding.
- A single Render instance is required for this demo because current frame and
  transcript state are intentionally in memory. The configuration therefore
  fixes `numInstances: 1`.
- **Known deployment dependency:** Render API credentials are valid. GitHub
  SSH authentication works on this laptop, but the installed GitHub CLI token
  has expired, so a GitHub repository must be created or the CLI reauthorized
  before Render can build the service from this source.
- **Demo security:** the requested public relay deliberately has no publisher
  authentication. Do not use its code/configuration for private footage, and
  rotate any API key that was shared in a chat after deployment.
