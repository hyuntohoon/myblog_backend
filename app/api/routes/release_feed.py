import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.me import provisioned_member_id
from app.api.schemas import ReleaseFeedItem, ReleaseFeedResponse
from app.db.session import get_db
from myblog_shared_db.models import Album, Artist, ArtistReleaseEvent, UserArtistTrack

logger = logging.getLogger(__name__)

router = APIRouter()

# RFC-locked rolling window for Recently Released (owner decision 2026-07-08).
RECENT_WINDOW_DAYS = 30

# Result-set guard: ordered by release_date asc, so truncation drops the
# farthest-future tail first, never the near window.
_FETCH_CAP = 1000

_KST = ZoneInfo("Asia/Seoul")

# Cross-repo twin: myblog_music/app/services/release_calendar_service.py
# (public calendar display soft-grouping). Keep key + preference rules in sync.
_SOURCE_PRIORITY = {"spotify": 0, "musicbrainz": 1, "itunes": 2}
_ITUNES_SUFFIXES = (" - single", " - ep")


def _kst_today() -> date:
    """Day boundary is KST wall-clock (site convention; DB stores UTC).

    Python-side (not func.current_date()) so a UTC Lambda session doesn't roll
    the day at 09:00 KST, and so the SQLite test harness behaves identically.
    """
    return datetime.now(_KST).date()


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
    today = _kst_today()

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

    upcoming: list[ReleaseFeedItem] = []
    recent: list[ReleaseFeedItem] = []
    for members in groups.values():
        item = _merge_group(members, today)
        if not _in_category(item, category):
            continue
        # Buckets tile the window completely: future → upcoming; past → recent;
        # release DAY itself goes to recent once confirmed released (the worker
        # confirms on day 0 — it must not vanish on its own release day), and
        # stays upcoming while still only announced.
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
