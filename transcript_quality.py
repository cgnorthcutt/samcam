"""Small, dependency-free safeguards for live speech recognition.

The body camera occasionally exposes a sustained feedback tone or repeats a
PCM buffer.  Whisper can turn either into a confident-looking, but obviously
wrong, transcript.  These checks run before text reaches the local API or the
public archive; they intentionally prefer dropping an implausible three-second
decode to saving invented speech.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable


_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_STUTTER_RUN = re.compile(r"([a-z0-9])(?:[^a-z0-9]*\1){11,}", re.IGNORECASE)


def normalize_transcript_text(text: object) -> str:
    """Return a stable comparison form without changing display text."""
    return " ".join(_WORD.findall(str(text).lower()))


def transcript_rejection_reason(text: object, duration_seconds: float | None = None) -> str | None:
    """Identify repetition patterns that cannot plausibly be live speech.

    This is deliberately conservative for short phrases: "testing, does this
    work" is retained, while long loops, character stutters, and a transcript
    far longer than its source audio are rejected.
    """
    raw = " ".join(str(text).split())
    normalized = normalize_transcript_text(raw)
    if not normalized:
        return "empty_text"
    if _STUTTER_RUN.search(raw):
        return "character_stutter"

    # A person can speak roughly 35 characters per second at the very fast end.
    # Give the decoder generous headroom, but never accept a paragraph from one
    # three-second audio window.
    if duration_seconds is not None and duration_seconds > 0:
        allowed_characters = max(180, int(duration_seconds * 65))
        if len(raw) > allowed_characters:
            return "impossibly_long_decode"

    words = normalized.split()
    if len(words) < 8:
        return None

    counts = Counter(words)
    if counts.most_common(1)[0][1] >= max(5, len(words) // 2):
        return "repeated_word"

    # Repeated two-to-four word phrases catch the familiar Whisper loop while
    # leaving a normal short sentence alone.  Three occurrences in one small
    # chunk are already much more likely a model loop than spoken language.
    for width in range(2, min(5, len(words) // 2 + 1)):
        phrases = [tuple(words[index:index + width]) for index in range(len(words) - width + 1)]
        if phrases and Counter(phrases).most_common(1)[0][1] >= 3:
            return "repeated_phrase"
    return None


def duplicate_transcript_reason(text: object, recent_texts: Iterable[str]) -> str | None:
    """Return a reason when the same accepted phrase is replayed by capture."""
    normalized = normalize_transcript_text(text)
    if normalized and normalized in set(recent_texts):
        return "duplicate_window"
    return None
