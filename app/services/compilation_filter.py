"""DATA-catalog-noise-and-lyrics-coverage Step 2 — read-side classical-compilation
filter (backend twin).

This is a byte-for-byte twin of ``myblog_music/app/services/compilation_filter.py``
— the canonical sibling. The two copies MUST stay in sync: this is duplicated
cross-repo code by design (the workspace CLAUDE.md twin-sweep rule — a fix to
``_COMP_TITLE_RE`` / ``is_compilation_noise`` lands in both repos in the same PR).
``_COMP_TITLE_RE`` and ``is_compilation_noise`` below are deliberately identical;
change one, change the other.

This copy guards ``GET /api/me/release-feed`` (``app/api/routes/release_feed.py``
— serves ``/radar/`` and the home ForYou strip): the third read path, after the
music home feed (``feed_service.py``) and the ``/releases/`` calendar
(``release_calendar_service.py``). Budget classical compilations ("065 Piano
Essentials", "Sunrise Prelude: Classical Masterpieces" volumes) enter the catalog
through Spotify album_ingest as ``album_type='album'`` credited to long-dead
composers, then crowd genuine new releases out of public surfaces. This module
hides them at *read time only* — the rows stay in the catalog, so the filter is
fully reversible and a mis-tuned predicate loses no data.

Detection is deliberately conservative to spare genuine classical *performances*
(a Mozart Requiem under a named conductor, an Argerich recital). A real
single-work performance credits at most a handful of artists and never carries a
budget-compilation label; verified against the 2026-07-24 window, all three
signals below fire only on compilations — zero real-performance false positives
(Mozart Requiem n=8/DG, Argerich Chopin n=3/Warner, Brahms·Karajan n=4/Warner,
Delius n=5/Warner, Orff Carmina n=8/Pentatone, LPO Strauss n=3 all pass through).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# Numbered budget series ("065 Piano Essentials", "038 Classical Momentum") plus
# the generic mood/programme compilation title families seen in the Spotify
# catalog on 2026-07-24. Word-boundaried and phrase-shaped so a bare "Classics"
# substring (e.g. inside a band or work name) is not enough — only the
# compilation phrasings match. None of the six real performances above hit this.
_COMP_TITLE_RE = re.compile(
    r"\b\d{2,3}\s+(?:piano\s+essentials|classical\s+momentum)\b"
    r"|\bclassical\s+(?:masterpieces|works|gems|essentials|momentum|reveries|"
    r"horizons|breeze|flowers|study|suite|tradition|listening\s+room|bangers)\b"
    r"|\b(?:bach|mozart|beethoven|chopin|debussy|brahms)\s*,[^&]*&\s*more\b",
    re.IGNORECASE,
)


def is_compilation_noise(
    *,
    title: Optional[str],
    label: Optional[str],
    n_artists: int,
    max_artists: int,
    budget_labels: Iterable[str],
) -> bool:
    """True if the album is a budget classical compilation to hide from public
    new-release surfaces. Any one signal is sufficient:

    1. credits >= ``max_artists`` distinct artists (real performances never do);
    2. carries a pure-compilation ``label`` (labels that only press comps);
    3. matches a known compilation title family (``_COMP_TITLE_RE``).
    """
    if n_artists >= max_artists:
        return True
    if label and label in set(budget_labels):
        return True
    if title and _COMP_TITLE_RE.search(title):
        return True
    return False
