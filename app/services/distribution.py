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


def split_artist_names(raw: Optional[str]) -> List[str]:
    """Split a denormalized ``', '``-joined artist string into individual names.

    This is the FALLBACK for the artist distribution — used only for an
    uncatalogued row (no ``album_id`` → no ``album_artists`` to join). It is
    lossy: a name that itself contains a comma (e.g. ``'Tyler, the Creator'``)
    is over-split. The catalogued path joins ``album_artists`` instead (one row
    per artist, exact), which is why it is preferred whenever ``album_id`` is
    present. Empty / whitespace-only fragments are dropped.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def expand_credits(
    rows: Iterable[Tuple[Optional[Iterable[str]], int]],
) -> Tuple[List[Optional[str]], List[int]]:
    """Expand ``(artist_names, weight)`` rows into per-artist ``(labels, weights)``
    for :func:`rank_counts`.

    Each artist in a collaboration is credited the row's FULL weight once —
    ``album_artists.role`` / ``track_artists.role`` are always NULL, so there is
    no primary/featured split to weight by. A row with no names yields a single
    ``(None, weight)`` credit so it tallies as unclassified. This is what
    de-fragments a collab that the denormalized text would otherwise bucket as a
    single ``'A, B'`` label: ``A`` and ``B`` each get their own credit.

    Note the partition invariant of :func:`rank_counts` (``sum(items) +
    unclassified == sum(weights)``) is over the EXPANDED weights, so for a set
    with collaborations ``sum(items)`` exceeds the raw row population — that
    over-count is the intended per-artist crediting, and callers keep ``total``
    as the true population (track / play count), not ``sum(weights)``.
    """
    labels: List[Optional[str]] = []
    weights: List[int] = []
    for names, weight in rows:
        clean = [n for n in (names or []) if n]
        w = int(weight)
        if clean:
            for name in clean:
                labels.append(name)
                weights.append(w)
        else:
            labels.append(None)
            weights.append(w)
    return labels, weights


VARIOUS_ARTISTS = "Various Artists"


def is_va_compilation(names: Optional[Iterable[str]]) -> bool:
    """True when an album's artist list is the Spotify 'Various Artists' compilation
    sentinel (its only artist is 'Various Artists'). The album-level credit then hides
    the real per-track performer, so the saved-track distribution skips it in the
    album_artists fallback (track_artists is the primary source — see
    :func:`resolve_saved_artist_names`)."""
    clean = [n for n in (names or []) if n]
    return bool(clean) and all(n == VARIOUS_ARTISTS for n in clean)


def resolve_saved_artist_names(
    track_names: Optional[Iterable[str]],
    album_names: Optional[Iterable[str]],
    denorm: Optional[str],
) -> List[str]:
    """Track-primary artist attribution for one saved (좋아요) track (FIX-analysis-
    artist-attribution): the track's own performers (track_artists) first — they
    capture track-level FEATURES and never collapse to the 'Various Artists' album
    sentinel — then album_artists (skipping a VA compilation), then splitting the
    denormalized artist_name (uncatalogued rows). Per-track grain is the faithful
    reading of a liked song; album_artists alone under-counts featured artists, which
    in a feature-heavy library is most of the set."""
    track = [n for n in (track_names or []) if n]
    if track:
        return track
    if album_names and not is_va_compilation(album_names):
        return [n for n in album_names if n]
    return split_artist_names(denorm)
