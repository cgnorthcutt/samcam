# Audio inspection notes

Use this guide to inspect an archived camera recording without changing it.
It is designed for experimentation with noisy miniature-camera microphones.

```bash
input="archives/<session-id>/recording.mp4"
ffprobe -v error -show_format -show_streams -of json "$input"
ffmpeg -hide_banner -i "$input" -map 0:a:0 -af volumedetect -f null -
ffmpeg -hide_banner -i "$input" -map 0:a:0 -af 'astats=metadata=1:reset=0' -f null -
ffmpeg -hide_banner -i "$input" -map 0:a:0 -af 'ebur128=peak=true' -f null -
```

Inspect the raw file first. Clipping, acoustic feedback, electrical hum, and a
low microphone sample rate are capture-time limitations; post-processing cannot
reconstruct detail that never made it into the original signal.

The public archive intentionally plays the original camera track. If you want
to experiment with a derived copy locally, use `restore_archive_audio.py` and
keep the source MP4 unchanged:

```bash
.venv/bin/python restore_archive_audio.py "$input" --dry-run
```

Listen to any derived result before sharing it. Do not apply an enhancement
pipeline automatically to known-good high-fidelity camera audio.
