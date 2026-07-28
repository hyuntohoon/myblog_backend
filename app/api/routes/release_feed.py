import logging
import uuid
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routes.me import provisioned_member_id
from app.api.schemas import ReleaseFeedItem, ReleaseFeedResponse
from app.core.config import settings
from app.core.kst import kst_today
from app.db.session import get_db
from app.services.compilation_filter import is_compilation_noise
from myblog_shared_db.models import (
    Album,
    Artist,
    ArtistReleaseEvent,
    UserArtistTrack,
    album_artists_table,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# RFC-locked rolling window for Recently Released (owner decision 2026-07-08).
RECENT_WINDOW_DAYS = 30

# Result-set guard: ordered by release_date asc, so truncation drops the
# farthest-future tail first, never the near window.
_FETCH_CAP = 1000

# Cross-repo twin: myblog_music/app/services/release_calendar_service.py
# (public calendar display soft-grouping). Keep key + preference rules in sync.
_SOURCE_PRIORITY = {"spotify": 0, "musicbrainz": 1, "itunes": 2}
_ITUNES_SUFFIXES = (" - single", " - ep")


def _normalize_title(title: str) -> str:
    """Soft-grouping key component (NOT stored): casefold + collapse whitespace
    + strip iTunes storefront ' - Single'/' - EP' suffixes."""
    norm = " ".join((title or "").split()).casefold()
    for suffix in _ITUNES_SUFFIXES:
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)].rstrip()
            break
    return norm


def _trust(status: str, spotify_album_id: Optional[str], release_date: date, today: date) -> str:
    """Derive the simple UI trust label from DB confirmation and timing."""
    if status == "released" or spotify_album_id is not None:
        return "확정"
    if status == "announced" and release_date >= today:
        return "예정"
    return "불확실"


def _merge_group(
    members: list[tuple[ArtistReleaseEvent, str]], today: date
) -> ReleaseFeedItem:
    """Collapse one soft group (same artist/date/normalized title observed by
    multiple sources) to one display item — mirrors the music-service twin:
    preferred source supplies the title, other fields coalesce, status is
    released if any member is."""
    members = sorted(
        members,
        key=lambda m: _SOURCE_PRIORITY.get(m[0].source, len(_SOURCE_PRIORITY)),
    )
    preferred, artist_name = members[0]
    release_type = next((e.release_type for e, _ in members if e.release_type), None)
    spotify_album_id = next(
        (e.spotify_album_id for e, _ in members if e.spotify_album_id), None
    )
    status = (
        "released"
        if any(e.status == "released" for e, _ in members)
        else "announced"
    )
    return ReleaseFeedItem(
        artist_id=preferred.artist_id,
        artist_name=artist_name,
        title=preferred.title,
        release_date=preferred.release_date,
        release_type=release_type,
        status=status,
        trust=_trust(status, spotify_album_id, preferred.release_date, today),
        spotify_album_id=spotify_album_id,
    )


def _attach_catalog_albums(db: Session, items: list[ReleaseFeedItem]) -> None:
    """FEAT-for-you-releases Step 1: link items to the catalog album (cover +
    overlay id) via their confirmed spotify_album_id — one IN-query for the
    whole page; announced-only events (no spotify id) stay bare."""
    spotify_ids = {i.spotify_album_id for i in items if i.spotify_album_id}
    if not spotify_ids:
        return
    rows = db.execute(
        select(Album.spotify_id, Album.id, Album.cover_url).where(
            Album.spotify_id.in_(spotify_ids)
        )
    ).all()
    by_spotify_id = {sid: (album_id, cover) for sid, album_id, cover in rows}
    for item in items:
        hit = by_spotify_id.get(item.spotify_album_id)
        if hit:
            item.album_id, item.cover_url = hit


def _album_noise_signals(
    db: Session, items: list[ReleaseFeedItem]
) -> dict[str, tuple[Optional[str], int]]:
    """DATA-catalog-noise-and-lyrics-coverage Step 2: batched lookup of
    (label, linked-artist count) for every item's resolved catalog album — one
    LEFT JOIN over the page, no per-item queries. Returns spotify_id →
    (label, n_artists); an album with no artist links gets n_artists=0. Distinct
    from ``_attach_catalog_albums`` (cover/id enrichment, runs after bucketing),
    which is left untouched here."""
    spotify_ids = {i.spotify_album_id for i in items if i.spotify_album_id}
    if not spotify_ids:
        return {}
    artist_count = func.count(album_artists_table.c.artist_id).label("n_artists")
    rows = db.execute(
        select(Album.spotify_id, Album.label, artist_count)
        .select_from(Album)
        .outerjoin(album_artists_table, album_artists_table.c.album_id == Album.id)
        .where(Album.spotify_id.in_(spotify_ids))
        .group_by(Album.id, Album.spotify_id, Album.label)
    ).all()
    return {sid: (label, count or 0) for sid, label, count in rows}


def _in_category(item: ReleaseFeedItem, category: str) -> bool:
    if category == "album":
        return item.release_type in ("album", "ep")
    if category == "single":
        return item.release_type == "single"
    return True


@router.get("/release-feed", response_model=ReleaseFeedResponse)
def get_release_feed(
    state: Literal["upcoming", "recent", "all"] = Query("all"),
    category: Literal["album", "single", "all"] = Query("all"),
    db: Session = Depends(get_db),
    member_id: uuid.UUID = Depends(provisioned_member_id),
) -> ReleaseFeedResponse:
    today = kst_today()

    # One fetch for both buckets: soft-grouping and the category filter must
    # see every source's observation of a release together — an iTunes
    # "X - Single" row and an MB "X" row (NULL type) are one release, and
    # per-bucket SQL filters would split the group across buckets.
    rows = db.execute(
        select(ArtistReleaseEvent, Artist.name.label("artist_name"))
        .select_from(ArtistReleaseEvent)
        .join(
            UserArtistTrack,
            (UserArtistTrack.artist_id == ArtistReleaseEvent.artist_id)
            & (UserArtistTrack.user_id == member_id),
        )
        .join(Artist, Artist.id == ArtistReleaseEvent.artist_id)
        .where(
            ArtistReleaseEvent.release_date
            >= today - timedelta(days=RECENT_WINDOW_DAYS)
        )
        .order_by(ArtistReleaseEvent.release_date.asc())
        .limit(_FETCH_CAP)
    ).all()

    groups: dict[tuple[uuid.UUID, date, str], list[tuple[ArtistReleaseEvent, str]]] = {}
    for event, artist_name in rows:
        key = (event.artist_id, event.release_date, _normalize_title(event.title))
        groups.setdefault(key, []).append((event, artist_name))

    # Merge each soft group to one item, then apply the category filter — both
    # must see the whole group together (see the fetch comment above).
    merged: list[ReleaseFeedItem] = []
    for members in groups.values():
        item = _merge_group(members, today)
        if not _in_category(item, category):
            continue
        merged.append(item)

    # DATA-catalog-noise-and-lyrics-coverage Step 2: drop budget classical
    # compilations before bucketing — twin of myblog_music's home feed +
    # /releases/ calendar (app/services/compilation_filter.py). label +
    # n_artists come from ONE batched catalog lookup; items with no
    # spotify_album_id or no catalog hit fall back to label=None, n_artists=0
    # (the title signal alone), exactly like the music twin's announced-only rows.
    noise_signals = _album_noise_signals(db, merged)
    budget_labels = frozenset(settings.COMP_FILTER_BUDGET_LABELS)
    max_artists = settings.COMP_FILTER_MAX_ARTISTS
    merged = [
        item
        for item in merged
        if not is_compilation_noise(
            title=item.title,
            label=noise_signals.get(item.spotify_album_id, (None, 0))[0],
            n_artists=noise_signals.get(item.spotify_album_id, (None, 0))[1],
            max_artists=max_artists,
            budget_labels=budget_labels,
        )
    ]

    # Bucket the survivors. Buckets tile the window completely: future →
    # upcoming; past → recent; release DAY itself goes to recent once confirmed
    # released (the worker confirms on day 0 — it must not vanish on its own
    # release day), and stays upcoming while still only announced.
    upcoming: list[ReleaseFeedItem] = []
    recent: list[ReleaseFeedItem] = []
    for item in merged:
        if item.release_date > today or (
            item.release_date == today and item.status != "released"
        ):
            upcoming.append(item)
        else:
            recent.append(item)

    upcoming.sort(key=lambda i: (i.release_date, i.artist_name))
    recent.sort(key=lambda i: (i.release_date, i.artist_name), reverse=True)

    if state == "upcoming":
        recent = []
    elif state == "recent":
        upcoming = []
    _attach_catalog_albums(db, upcoming + recent)
    return ReleaseFeedResponse(upcoming=upcoming, recent=recent)
