# app/services/artist_primary.py
# RFC-ui-surface-unification Step 4 — deterministic primary-artist pick.
#
# Mirrors musicApi's AlbumRepository.get_primary_artist_map (BUG-19 Q1 (a)):
# order by (popularity DESC NULLS LAST, name ASC) and keep the first row, so
# the same album presents the same primary artist across services and reloads.
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from myblog_shared_db.models import Artist, album_artists_table


def pick_primary_artist(artists: Iterable[Artist]) -> Optional[Artist]:
    """In-memory pick for rows whose album.artists are already eager-loaded
    (saved-tracks list) — avoids a second query per page."""
    ranked = sorted(
        artists,
        key=lambda a: (a.popularity is None, -(a.popularity or 0), a.name or ""),
    )
    return ranked[0] if ranked else None


def primary_artist_map(db: Session, album_ids: List) -> Dict[str, Tuple[str, str]]:
    """Batch resolve {album_id(str): (artist_id(str), artist_name)} for rows
    that carry only an album_id column (daily picks, member review feed).
    `album_ids` are the raw column values (UUIDs) — no str() coercion here so
    the PGUUID IN() comparison stays native."""
    if not album_ids:
        return {}
    rows = db.execute(
        select(album_artists_table.c.album_id, Artist.id, Artist.name)
        .join(Artist, album_artists_table.c.artist_id == Artist.id)
        .where(album_artists_table.c.album_id.in_(album_ids))
        .order_by(Artist.popularity.desc().nullslast(), Artist.name.asc())
    ).all()
    out: Dict[str, Tuple[str, str]] = {}
    for al_id, ar_id, ar_name in rows:
        key = str(al_id)
        if key not in out:
            out[key] = (str(ar_id), ar_name)
    return out
