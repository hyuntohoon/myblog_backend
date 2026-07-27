"""Anchor Genius annotations onto our normalized lyric segments.

FEAT-lyrics-annotations Thread 1. RFC: docs/rfcs/FEAT-lyrics-annotations.md §6.6.

**The problem.** Our lyrics come from LRCLIB as timestamped LRC, which breaks lines
where the vocal breathes. Genius breaks lines where the *sentence* ends, so the same
passage looks different on the two sides::

    Genius   "En el primero, sexo, violencia y llantas"
    our LRC  "sexo violencia y llantas"

Comparing line to line therefore fails on most annotations — measured at 33%, which is
a matcher artifact and not a property of the data. This module drops line boundaries
instead: concatenate every segment into one normalized string while remembering which
segment each character came from, find the Genius fragment as a plain substring, then
map the matched character span back to an inclusive ``(start_i, end_i)`` segment range.
A fragment covering three of our segments simply resolves to three.

Measured on ROSALÍA's *LUX* (13 tracks with synced lyrics, 103 annotations, 95 of them
lyric-bound): **74.7% resolve to a single span, +20.0% are chorus repeats whose location
is known, 5.3% unmatched** — 94.7% placeable.

**Anchoring is a pure function over the current lyric text and is never stored.**
Persisting the resolved offsets would silently rot the first time ``lyrics_reassessment``
re-matches a track against a different LRCLIB upload; recomputing costs microseconds and
self-heals. It also means the annotation store does not depend on lyrics existing at all:
a track with no segments yields all-``unmatched`` rather than an error, which is what lets
the annotation feature ship independently of the lyrics viewer.

The workspace tool ``tools/genius_anchor.py`` mirrors this module for batch measurement
and carries a parity self-test against it. **This file is the authority** — a change here
must be reflected there in the same PR.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from app.api.schemas import LyricsSegment

# A Genius fragment that is only a structural marker — "[Verso 1]", "[Outro Instrumental]".
# These annotate a *section*, not a line, and LRC never carries them, so they are
# classified separately instead of being reported as failures.
_SECTION_RE = re.compile(r"^\s*\[[^\]]{1,40}\]\s*$")

# Kept deliberately wide: Latin, Cyrillic, Arabic, Kana/Han, Hangul. LUX alone spans at
# least 11 languages, so a Latin-only filter would silently drop whole tracks.
_KEEP_RE = re.compile(r"[^0-9a-zЀ-ӿ؀-ۿ぀-ヿ一-鿿가-힯 ]")

# Head-of-fragment fallback widths, longest first.
_HEAD_WIDTHS = (8, 6, 4)


def normalize(text: str) -> str:
    """Fold to a comparison form: strip accents/case/punctuation, collapse whitespace.

    Accent stripping matters — LRCLIB uploads are crowd-typed and inconsistent about
    diacritics ("De Madruga" vs "De Madrugá") while Genius is usually correct.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(_KEEP_RE.sub(" ", text.lower()).split())


@dataclass
class _Haystack:
    text: str
    owner: List[int]  # owner[c] = the segment index character c came from


def _build_haystack(segments: Sequence[LyricsSegment]) -> _Haystack:
    """Concatenate the segments, remembering the owning segment of every character.

    Ownership is by ``segment.i`` — the index the viewer keys on — not by list
    position, so a span's numbers name the same rows the reader sees. Segments that
    normalize to nothing (gap rows) contribute no characters but still hold their index.
    """
    parts: List[str] = []
    owner: List[int] = []
    for segment in segments:
        norm = normalize(segment.text)
        if not norm:
            continue
        if parts:                      # single space joins segments; attribute it forward
            parts.append(" ")
            owner.append(segment.i)
        parts.append(norm)
        owner.extend([segment.i] * len(norm))
    return _Haystack("".join(parts), owner)


@dataclass
class Anchor:
    """Where one annotation lands. ``span`` is None when it could not be placed."""

    index: int
    status: str                                   # unique | repeated | partial | section | unmatched
    span: Optional[Tuple[int, int]] = None        # inclusive segment-index range
    occurrences: int = 0

    @property
    def placed(self) -> bool:
        return self.status in ("unique", "partial")


def _locate(hay: _Haystack, needle: str) -> Optional[Tuple[Tuple[int, int], int]]:
    starts = [m.start() for m in re.finditer(re.escape(needle), hay.text)]
    if not starts:
        return None
    end = min(starts[0] + len(needle) - 1, len(hay.owner) - 1)
    return (hay.owner[starts[0]], hay.owner[end]), len(starts)


def anchor_fragments(
    segments: Sequence[LyricsSegment], fragments: Iterable[str]
) -> List[Anchor]:
    """Locate each Genius fragment inside our lyric segments, in input order.

    Statuses:
      ``unique``    exactly one occurrence — safe to render inline on that span
      ``repeated``  several occurrences (a chorus) — render on the first, note the repeat
      ``partial``   only the fragment's head matched; the span is approximate
      ``section``   a structural marker, not a lyric line
      ``unmatched`` genuinely absent from our text

    Empty ``segments`` yields all-``unmatched`` rather than raising — that is the
    no-lyrics case, and callers rely on it degrading quietly.
    """
    hay = _build_haystack(segments)
    results: List[Anchor] = []

    for i, raw in enumerate(fragments):
        raw = raw or ""
        if _SECTION_RE.match(raw.strip()):
            results.append(Anchor(i, "section"))
            continue

        needle = normalize(" ".join(raw.split("\n")))
        if not needle or not hay.text:
            results.append(Anchor(i, "unmatched"))
            continue

        hit = _locate(hay, needle)
        if hit is not None:
            span, count = hit
            results.append(Anchor(
                i, "unique" if count == 1 else "repeated", span=span, occurrences=count
            ))
            continue

        # Head-of-fragment fallback. Genius sometimes carries an ad-lib or a trailing
        # word our upload omits ("...yo siempre lo doy, uh-uh"), which breaks the
        # whole-fragment match while the opening still pins the location unambiguously.
        words = needle.split()
        placed: Optional[Anchor] = None
        for width in _HEAD_WIDTHS:
            if len(words) < width:
                continue
            hit = _locate(hay, " ".join(words[:width]))
            if hit is not None:
                span, count = hit
                placed = Anchor(
                    i, "partial" if count == 1 else "repeated", span=span, occurrences=count
                )
                break
        results.append(placed or Anchor(i, "unmatched"))

    return results
