# Audio forensics: Curtis · Jul 30, 9:16 AM

## Scope

This is a read-only investigation of one public archive recording. No application, relay, capture, or archive code was changed. The downloaded input and trial outputs stayed under `/private/tmp`.

## Exact target

| Field | Value |
| --- | --- |
| Public session | `Curtis-20260730T161648Z-b54a5006` |
| Public MP4 | `https://samcam.app/archive/Curtis-20260730T161648Z-b54a5006/video.mp4` |
| Start time | 2026-07-30 09:16:48 PDT (16:16:48 UTC) |
| Duration | 76.414 s container duration; 76.350 s video duration |
| Downloaded MP4 SHA-256 | `e007c33ba51390583ad4eca8dbef220cc8ce8f95d76076961d65739cba3857a5` |

## What is in the MP4

`ffprobe` reports a real, continuous AAC audio stream, not a silent or missing audio track:

| Property | Measured value |
| --- | --- |
| Codec | AAC-LC |
| Capture/encode format | 16,000 Hz, mono, approximately 52.9 kb/s |
| Audio packets | 1,194 packets, normally 64 ms each |
| Packet-timestamp gaps over 65 ms | 0 |
| Tail | 1.49 s of silence; final AAC packets are tiny (4 bytes), consistent with silence rather than a transport gap |

The 16 kHz source has an 8 kHz Nyquist ceiling. It cannot retain the upper speech detail that makes a final recording sound crisp; transcoding it later cannot restore that information.

## Measured defects

| Finding | Evidence | Likely audible effect |
| --- | --- | --- |
| **Overload / clipping risk** | Decoded samples range from -1.027 to +1.005; `ebur128` measured **+3.2 dBFS true peak**. `volumedetect` found 420 samples in its 0 dB bin. | Crackle, raspy consonants, and distortion on loud speech or feedback peaks. |
| **Strong 60/120 Hz energy** | Narrow 55–65 Hz and 115–125 Hz band tests peak at -11.9 and -11.0 dBFS. | Low buzz/hum; materially louder than acceptable background electrical noise. |
| **Low-frequency dominance and DC bias** | 20–250 Hz mean is -27.5 dB versus -32.2 dB for 250–3400 Hz. DC offset is 0.007436. | Rumble, low wash, reduced speech intelligibility, and less headroom. |
| **Full-scale high-band events** | The 3.4–7.5 kHz band averages -42.0 dB but reaches 0.0 dBFS. | Isolated hiss/screech/click events. This is consistent with feedback or overload, but the file alone cannot prove an acoustic feedback loop. |
| **Excessive loudness swing** | Integrated loudness is -21.4 LUFS; loudness range is **23.6 LU**. | Quiet speech disappears under noise while louder moments suddenly distort. |
| **Lossy/narrow capture format** | AAC-LC at 16 kHz mono / ~53 kb/s. | No post-process can recover natural high-frequency clarity or stereo separation. |

The audio packet timeline is clean, so this evidence does **not** support a packet-loss or MP4-muxing explanation for the crackle. The leading causes are source overload/feedback, low-frequency contamination, and the 16 kHz capture format.

## Temporary restoration experiment (not deployed)

A temporary WAV was created only to establish that basic signal safety can be improved without touching the app. It used FFmpeg's built-in declipper, high-pass, low-pass, adaptive FFT denoiser, limiter, and one-pass loudness normalizer. This is not a final delivery recipe: one-pass `loudnorm` upsampled the test WAV to 192 kHz and must not be mistaken for recovered audio quality.

| Metric | Original archive MP4 | Temporary restoration test |
| --- | ---: | ---: |
| True peak | +3.2 dBFS | -1.5 dBFS |
| Decoded DC offset | 0.007436 | -0.000012 |
| Loudness range | 23.6 LU | 11.4 LU |
| Peak ceiling | Exceeded | Held below the requested -1.5 dBFS ceiling |

The trial proves that over-level and DC problems are measurable and can be contained. It does **not** prove that speech is pristine: aggressive denoising or declipping can create metallic artifacts, and no filter can restore detail discarded by 16 kHz capture. Any production choice needs A/B listening on clean headphones.

## Reproduce the inspection

Run these commands from a disposable directory. They only download and inspect the public recording.

```bash
session_id='Curtis-20260730T161648Z-b54a5006'
workdir="$(mktemp -d /private/tmp/samcam-audio-forensics.XXXXXX)"
curl -fsSL "https://samcam.app/archive/${session_id}/video.mp4" -o "$workdir/${session_id}.mp4"
shasum -a 256 "$workdir/${session_id}.mp4"

ffprobe -v error -show_format -show_streams -of json "$workdir/${session_id}.mp4"
ffmpeg -hide_banner -i "$workdir/${session_id}.mp4" -map 0:a:0 -af volumedetect -f null -
ffmpeg -hide_banner -i "$workdir/${session_id}.mp4" -map 0:a:0 -af 'astats=metadata=1:reset=0' -f null -
ffmpeg -hide_banner -i "$workdir/${session_id}.mp4" -map 0:a:0 -af 'ebur128=peak=true' -f null -
```

Check AAC continuity separately:

```bash
ffprobe -v error -select_streams a:0 -show_entries packet=pts_time,duration_time,size -of csv=p=0 "$workdir/${session_id}.mp4" > "$workdir/audio-packets.csv"
awk -F, 'NR > 1 { delta=$1-prev; if (delta > 0.065) gaps++ } { prev=$1 } END { print "gaps over 65 ms:", gaps+0 }' "$workdir/audio-packets.csv"
```

Measure low-frequency and hum bands:

```bash
for filter in 'highpass=f=20,lowpass=f=250' 'highpass=f=250,lowpass=f=3400' 'bandpass=f=60:w=10' 'bandpass=f=120:w=10' 'highpass=f=3400,lowpass=f=7500'; do
  ffmpeg -hide_banner -i "$workdir/${session_id}.mp4" -map 0:a:0 -af "$filter,volumedetect" -f null -
done
```

## Candidate offline-final pipeline to evaluate

This command is a **temporary evaluation path only**, not an application change. It addresses the measured defects before a final MP4 is encoded. Retain the lossless WAV while evaluating; encode AAC only at the end.

```bash
ffmpeg -y -i "$workdir/${session_id}.mp4" -map 0:a:0 -af 'adeclip=w=55:o=75:a=8:t=10,highpass=f=70,lowpass=f=6800,afftdn=nr=12:nf=-40:tn=1:gs=8,alimiter=limit=0.8414:level=disabled' -c:a pcm_s24le "$workdir/restored.wav"
```

For final production quality, evaluate these open-source components against the raw capture **before** AAC encoding:

- **FFmpeg `adeclip` + `afftdn`** for bounded declipping and stationary noise.
- **RNNoise** or **DeepFilterNet** for speech-targeted denoising; choose the lower-artifact result through blinded A/B listening, not a numerical score alone.
- **WebRTC Audio Processing** / SpeexDSP echo cancellation at capture time for the live path. Post-processing an already-recorded feedback loop cannot reliably separate the talker and speaker from one microphone signal.
- **Two-pass `loudnorm`** plus a true-peak limiter for the exported MP4, aiming for -16 LUFS stereo-equivalent presentation, -1.5 dBTP maximum, and a speech-appropriate loudness range around 7–12 LU.

## Acceptance tests before shipping an audio pipeline

1. Capture a controlled spoken phrase at quiet, normal, and loud levels with the live speakers both muted and enabled.
2. Preserve the capture as 48 kHz (or the hardware's native higher rate), PCM/FLAC during processing; do not begin at 16 kHz AAC.
3. Reject a recording if packet PTS gaps exceed one expected audio frame, if true peak exceeds -1.0 dBTP, or if decoded samples exceed full scale.
4. Measure 60 Hz and 120 Hz bands against a clean reference; set the exact threshold after calibration, then fail the test when hum returns.
5. Run blind A/B headphone listening for crackle, buzz, pumping, metallic denoising, and feedback. Metrics alone are insufficient.
6. For the live path, verify that enabling public playback cannot re-enter the capture microphone. Headphones or capture-side echo cancellation are the reliable controls; final-MP4 cleanup is separate.
