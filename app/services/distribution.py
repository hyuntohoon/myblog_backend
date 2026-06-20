# app/services/distribution.py
# FEAT-genre-artist-distribution Step 3 — the ONE shared counting primitive used by
# both the saved-tracks source and the play-events source (통일성). Pure: it takes
# already-resolved labels (+ optional weights) and returns a ranked partition. The
# genre/artist resolution (track_genres → album_genres, album artist) lives in
# LibraryService; this module only counts, so it is fully unit-testable with no DB.
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple


def rank_counts(
    labels: Iterable[Optional[str]],
    weights: Optional[Iterable[int]] = None,
) -> Tuple[List[Tuple[str, int]], int]:
    """Tally ``labels`` into a ranked ``[(label, count)]`` partition plus an
    ``unclassified_count``.

    - ``labels``: one entry per item; ``None`` means unclassified (no resolved
      genre / no artist).
    - ``weights``: optional parallel iterable of ints (default 1 each) — used by the
      play-events source to weight each album by its play_count.

    Items are sorted by count desc then label asc (stable, deterministic). The result
    is a clean partition: ``sum(count for _, count in items) + unclassified_count ==
    sum(weights)``.
    """
    labels = list(labels)
    weights = [1] * len(labels) if weights is None else [int(w) for w in weights]
    if len(weights) != len(labels):
        raise ValueError("labels and weights must be the same length")

    counts: dict[str, int] = {}
    unclassified = 0
    for label, weight in zip(labels, weights):
        if label is None:
            unclassified += weight
        else:
            counts[label] = counts.get(label, 0) + weight

    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return items, unclassified
