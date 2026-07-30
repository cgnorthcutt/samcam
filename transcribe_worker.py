#!/usr/bin/env python3
"""Capture GENERAL - AUDIO and emit local Whisper transcripts as JSON lines."""

from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import sys
import threading
import time
from collections import deque

import numpy as np
import mlx_whisper

from transcript_quality import (
    duplicate_transcript_reason,
    normalize_transcript_text,
    transcript_rejection_reason,
)

MODEL = os.environ.get(
    "BODYCAM_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"
)
LANGUAGE = os.environ.get("BODYCAM_TRANSCRIPT_LANGUAGE", "en")
CHUNK_SECONDS = float(os.environ.get("BODYCAM_TRANSCRIPT_CHUNK_SECONDS", "3"))
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
CHUNK_BYTES = int(CHUNK_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE)
MIN_RMS_DB = float(os.environ.get("BODYCAM_TRANSCRIPT_MIN_RMS_DB", "-52"))
MIN_AVG_LOGPROB = float(os.environ.get("BODYCAM_TRANSCRIPT_MIN_AVG_LOGPROB", "-1.0"))
MAX_NO_SPEECH_PROB = float(os.environ.get("BODYCAM_TRANSCRIPT_MAX_NO_SPEECH_PROB", "0.45"))
MAX_COMPRESSION_RATIO = float(os.environ.get("BODYCAM_TRANSCRIPT_MAX_COMPRESSION_RATIO", "2.2"))
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


def noisy_feedback_reason(samples: np.ndarray, rms_db: float) -> str | None:
    """Reject a loud, narrow high-frequency tone before Whisper hallucinates.

    Voice spreads energy across many bins. A speaker-to-camera feedback loop
    is typically dominated by one high-frequency bin, even when it is loud
    enough to pass a simple RMS silence check.
    """
    if len(samples) < 1024 or rms_db < -35.0:
        return None
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    if len(spectrum) < 3:
        return None
    total = float(np.sum(spectrum[1:]))
    if total <= 0:
        return None
    peak_index = int(np.argmax(spectrum[1:])) + 1
    peak_hz = peak_index * SAMPLE_RATE / len(samples)
    peak_share = float(spectrum[peak_index] / total)
    high_start = int(4_800 * len(samples) / SAMPLE_RATE)
    high_share = float(np.sum(spectrum[high_start:]) / total) if high_start < len(spectrum) else 0.0
    if peak_hz >= 1_600 and peak_share >= 0.34:
        return "tonal_feedback"
    if high_share >= 0.78:
        return "high_frequency_feedback"
    return None


def whisper_quality_reason(result: dict) -> str | None:
    """Use Whisper's own no-speech and repetition diagnostics, when present."""
    segments = result.get("segments") or []
    no_speech = [float(item["no_speech_prob"]) for item in segments if item.get("no_speech_prob") is not None]
    compression = [float(item["compression_ratio"]) for item in segments if item.get("compression_ratio") is not None]
    if no_speech and max(no_speech) >= MAX_NO_SPEECH_PROB:
        return "no_speech_probability"
    if compression and max(compression) >= MAX_COMPRESSION_RATIO:
        return "repetitive_decode"
    return None


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
    recent_emitted_texts: deque[str] = deque(maxlen=8)
    recent_window_hashes: deque[bytes] = deque(maxlen=8)
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
        window_hash = hashlib.blake2s(raw, digest_size=12).digest()
        if window_hash in recent_window_hashes:
            emit({"type": "silence", "started": chunk_start, "ended": audio_time, "rms_db": round(rms_db, 1), "reason": "duplicate_pcm_window"})
            continue
        recent_window_hashes.append(window_hash)
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
        feedback_reason = noisy_feedback_reason(samples, rms_db)
        if feedback_reason:
            emit({"type": "silence", "started": chunk_start, "ended": audio_time, "rms_db": round(rms_db, 1), "reason": feedback_reason})
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
        normalized = normalize_transcript_text(text)
        segments = result.get("segments") or []
        logprobs = [
            float(segment["avg_logprob"])
            for segment in segments
            if segment.get("avg_logprob") is not None
        ]
        average_logprob = (
            sum(logprobs) / len(logprobs) if logprobs else None
        )
        quality_reason = whisper_quality_reason(result)
        text_reason = transcript_rejection_reason(text, duration)
        duplicate_reason = duplicate_transcript_reason(text, recent_emitted_texts)
        known_silence_hallucination = (
            normalized in COMMON_SILENCE_HALLUCINATIONS
            and (
                rms_db < -30.0
                or average_logprob is None
                or average_logprob < -0.30
            )
        )
        low_confidence = (
            average_logprob is not None
            and average_logprob < MIN_AVG_LOGPROB
        )
        has_spoken_characters = any(character.isalnum() for character in text)
        rejection_reason = (
            "low_confidence"
            if low_confidence
            else "known_hallucination"
            if known_silence_hallucination
            else quality_reason
            or text_reason
            or duplicate_reason
            or "no_spoken_characters"
        )

        if (
            has_spoken_characters
            and not low_confidence
            and not known_silence_hallucination
            and not quality_reason
            and not text_reason
            and not duplicate_reason
        ):
            recent_emitted_texts.append(normalized)
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
                    "no_speech_prob": max(
                        (
                            float(segment["no_speech_prob"])
                            for segment in segments
                            if segment.get("no_speech_prob") is not None
                        ),
                        default=None,
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
                    "reason": rejection_reason,
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
