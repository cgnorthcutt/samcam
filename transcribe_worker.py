#!/usr/bin/env python3
"""Capture GENERAL - AUDIO and emit local Whisper transcripts as JSON lines."""

from __future__ import annotations

import json
import math
import os
import queue
import sys
import threading
import time

import numpy as np
import mlx_whisper

MODEL = os.environ.get(
    "BODYCAM_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"
)
LANGUAGE = os.environ.get("BODYCAM_TRANSCRIPT_LANGUAGE", "en")
CHUNK_SECONDS = float(os.environ.get("BODYCAM_TRANSCRIPT_CHUNK_SECONDS", "3"))
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
CHUNK_BYTES = int(CHUNK_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE)
MIN_RMS_DB = float(os.environ.get("BODYCAM_TRANSCRIPT_MIN_RMS_DB", "-52"))
QUEUED_CHUNKS = int(os.environ.get("BODYCAM_TRANSCRIPT_QUEUED_CHUNKS", "6"))
COMMON_SILENCE_HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe",
}


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def main() -> None:
    emit({"type": "status", "state": "starting", "model": MODEL})
    emit(
        {
            "type": "status",
            "state": "capturing",
            "model": MODEL,
            "device": "GENERAL - AUDIO",
            "chunk_seconds": CHUNK_SECONDS,
        }
    )

    # Keep draining the camera pipe while MLX is decoding the previous chunk.
    # Otherwise a full OS pipe can backpressure the combined FFmpeg process and
    # freeze the live video endpoint. If inference ever falls far behind, drop
    # the oldest speech rather than adding an ever-growing live delay.
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUED_CHUNKS)

    def read_audio() -> None:
        while True:
            raw = read_exact(sys.stdin.buffer, CHUNK_BYTES)
            if not raw:
                break
            try:
                chunks.put_nowait(raw)
            except queue.Full:
                try:
                    chunks.get_nowait()
                except queue.Empty:
                    pass
                chunks.put_nowait(raw)
        chunks.put(None)

    threading.Thread(target=read_audio, daemon=True).start()

    audio_time = 0.0
    last_emitted_text = ""
    while True:
        # The server owns the camera's one combined AVFoundation process and
        # pipes its 16 kHz mono PCM here. Opening GENERAL - AUDIO in a second
        # process stalls this firmware's GENERAL - UVC video endpoint.
        raw = chunks.get()
        if raw is None:
            return

        duration = len(raw) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        samples_i16 = np.frombuffer(raw, dtype=np.int16)
        samples = samples_i16.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        rms_db = 20.0 * math.log10(max(rms, 1e-9))
        chunk_start = audio_time
        audio_time += duration

        if duration < CHUNK_SECONDS * 0.75:
            continue
        if rms_db < MIN_RMS_DB:
            emit(
                {
                    "type": "silence",
                    "started": chunk_start,
                    "ended": audio_time,
                    "rms_db": round(rms_db, 1),
                }
            )
            continue

        started = time.monotonic()
        result = mlx_whisper.transcribe(
            samples,
            path_or_hf_repo=MODEL,
            language=LANGUAGE,
            task="transcribe",
            verbose=None,
            temperature=0,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            word_timestamps=False,
        )
        latency = time.monotonic() - started
        text = " ".join(result.get("text", "").split())
        normalized = "".join(
            character.lower()
            for character in text
            if character.isalnum() or character.isspace()
        ).strip()
        segments = result.get("segments") or []
        logprobs = [
            float(segment["avg_logprob"])
            for segment in segments
            if segment.get("avg_logprob") is not None
        ]
        average_logprob = (
            sum(logprobs) / len(logprobs) if logprobs else None
        )
        known_silence_hallucination = (
            normalized in COMMON_SILENCE_HALLUCINATIONS
            and (average_logprob is None or average_logprob < -0.30)
        )
        repeated_hallucination = (
            bool(normalized)
            and normalized == last_emitted_text
            and (average_logprob is None or average_logprob < -0.35)
        )

        if text and not known_silence_hallucination and not repeated_hallucination:
            last_emitted_text = normalized
            emit(
                {
                    "type": "transcript",
                    "text": text,
                    "started": round(chunk_start, 2),
                    "ended": round(audio_time, 2),
                    "latency": round(latency, 2),
                    "rms_db": round(rms_db, 1),
                    "language": result.get("language", LANGUAGE),
                    "avg_logprob": (
                        round(average_logprob, 3)
                        if average_logprob is not None
                        else None
                    ),
                }
            )
        else:
            emit(
                {
                    "type": "silence",
                    "started": chunk_start,
                    "ended": audio_time,
                    "rms_db": round(rms_db, 1),
                    "reason": (
                        "known_hallucination"
                        if known_silence_hallucination
                        else "repeated_hallucination"
                        if repeated_hallucination
                        else "no_text"
                    ),
                }
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        emit({"type": "error", "state": "error", "error": str(exc)})
        raise
