# app/api/routes/library.py
import logging
import uuid
from datetime import datetime
from typing import Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.schemas import (
    AddToListenRequest,
    AlbumBrief,
    ClassifyResponse,
    DistItem,
    DistributionResponse,
    FillGenresResponse,
    ListenedAlbumItem,
    ListenedAlbumsResponse,
    NowPlayingResponse,
    RecentlyListenedItem,
    RecentlyListenedResponse,
    RecentTrackItem,
    RecentTracksResponse,
    RefreshRecentResponse,
    ReviewedAlbumResponse,
    ReviewedResponse,
    SavedTrackItem,
    SavedTracksResponse,
    OnThisDayItem,
    RetroYearStat,
    RetrospectiveResponse,
    SpotifyConnectionResponse,
    StreamAlbumRankItem,
    StreamAlbumRankResponse,
    StreamClockCell,
    StreamClockResponse,
    StreamItemDetailResponse,
    StreamRankItem,
    StreamRankResponse,
    ToListenItemResponse,
    ToListenReorderRequest,
    ToListenResponse,
    UnclassifiedBreakdown,
)
from app.api.routes.me import provisioned_member_id
from app.clients.sqs_client import get_spotify_connection_status
from app.core.authz import require_owner
from app.db.session import get_db
from app.di import get_library_service, get_sqs_client
from app.services.artist_primary import pick_primary_artist
from app.services.enqueue import safe_enqueue_catalog_sync
from app.services.genre_service import GenreService
from app.services.library_service import (
    AlbumNotFoundError,
    DuplicateItemError,
    ItemNotFoundError,
    LibraryService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _album_brief(album, genres=None) -> AlbumBrief:
    return AlbumBrief(
        id=str(album.id),
        title=album.title,
        cover_url=album.cover_url,
        release_date=album.release_date,
        popularity=album.popularity,
        artist_names=[a.name for a in album.artists],
        genres=genres or [],
    )


def _saved_track_artist_id(row) -> Optional[str]:
    """ui-unify Step 4: primary catalog-artist id for a saved-track row —
    in-memory pick over the eager-loaded .album.artists (no extra query);
    None when the track's album isn't in the catalog."""
    if row.album is None:
        return None
    primary = pick_primary_artist(row.album.artists)
    return str(primary.id) if primary is not None else None


def _to_listen_response(item) -> ToListenItemResponse:
    return ToListenItemResponse(
        id=str(item.id),
        album_id=str(item.album_id),
        position=item.position,
        note=item.note,
        added_at=item.added_at,
        album=_album_brief(item.album),
    )


# ── reads (edge_guard only — no JWT; covered by GET /api/{proxy+}) ──────────────

# FEAT-multi-user Phase 2: the to-listen queue is per-user, so this read is now
# member-scoped (require_cognito_token via provisioned_member_id) — was edge_guard.
@router.get("/to-listen", response_model=ToListenResponse)
def list_to_listen(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    items = svc.list_to_listen(db, member_id)
    return ToListenResponse(items=[_to_listen_response(it) for it in items])


@router.get("/reviewed", response_model=ReviewedResponse)
def list_reviewed(
    group_by: str = "album",
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    # group_by is fixed to "album" for now (D20); accepted as a query param so the
    # contract is forward-compatible if a by-review grouping is ever added.
    rows = svc.list_reviewed(db)
    return ReviewedResponse(
        items=[
            ReviewedAlbumResponse(
                album_id=str(album.id),
                review_ids=review_ids,
                album=_album_brief(album),
            )
            for album, review_ids in rows
        ]
    )


@router.get("/recently-listened", response_model=RecentlyListenedResponse)
def list_recently_listened(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    # 최근 들은 앨범 (D25/D26) — read from the worker-fed cache; never calls Spotify.
    rows = svc.list_recently_listened(db)
    return RecentlyListenedResponse(
        items=[
            RecentlyListenedItem(
                album_id=str(album.id),
                last_played_at=last_played_at,
                album=_album_brief(album),
            )
            for album, last_played_at in rows
        ],
        last_synced_at=svc.last_recent_synced_at(db),
    )


@router.get("/recent-tracks", response_model=RecentTracksResponse)
def list_recent_tracks(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    # 최근 재생 트랙 (D-B) — worker-fed rolling cache; never calls Spotify. Row 0 is
    # also the "최근 재생" latest-played fallback for the now-playing surface (D-C).
    rows = svc.list_recent_tracks(db)
    return RecentTracksResponse(
        items=[
            RecentTrackItem(
                spotify_track_id=r.spotify_track_id,
                track_name=r.track_name,
                artist_name=r.artist_name,
                album_name=r.album_name,
                album_id=str(r.album_id) if r.album_id else None,
                album=_album_brief(r.album) if r.album is not None else None,
                played_at=r.played_at,
            )
            for r in rows
        ],
        last_synced_at=svc.last_recent_tracks_synced_at(db),
    )


@router.get("/listened-albums", response_model=ListenedAlbumsResponse)
def list_listened_albums(
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    # 들은 앨범(누적) (D-A) — aggregate of the append-only spotify_play_events log
    # (per-album play_count + first/last play); no rollup table. DB-only read.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows, total = svc.list_listened_albums(db, limit=limit, offset=offset)
    return ListenedAlbumsResponse(
        items=[
            ListenedAlbumItem(
                album_id=str(album.id),
                play_count=play_count,
                first_played_at=first_played_at,
                last_played_at=last_played_at,
                album=_album_brief(album),
            )
            for album, play_count, first_played_at, last_played_at in rows
        ],
        total=total,
    )


@router.get("/now-playing", response_model=NowPlayingResponse)
def now_playing(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    np = svc.get_now_playing(db)
    if np is None or not np.is_playing:
        # Idle still carries updated_at so the UI can show "동기화 N분 전"
        # instead of asserting liveness (D28). None when no snapshot exists yet.
        return NowPlayingResponse(
            is_playing=False,
            updated_at=np.updated_at if np else None,
        )
    return NowPlayingResponse(
        is_playing=True,
        track=np.track_name,
        artist=np.artist_name,
        album=np.album_name,
        album_id=str(np.album_id) if np.album_id else None,
        # np.album lazy-loads the catalog Album (FK); None when not in our catalog.
        album_cover_url=np.album.cover_url if np.album is not None else None,
        updated_at=np.updated_at,
    )


@router.get("/spotify-connection", response_model=SpotifyConnectionResponse)
def spotify_connection():
    # Status for the 연동 tab: token validity, not mere presence (D30). needs_reauth is
    # flipped by the worker on an invalid_grant; the front then shows "재인증 필요".
    st = get_spotify_connection_status()
    return SpotifyConnectionResponse(
        connected=st.connected,
        needs_reauth=st.needs_reauth,
        last_successful_refresh_at=st.last_successful_refresh_at,
    )


# ── 분석 버킷: saved-tracks + genre/artist distribution (FEAT-genre-artist-distribution) ──
# Reads are edge_guard-only (no JWT), matching the other library reads. Both the 좋아요
# source and the 재생 (play-events) source return the SAME DistributionResponse so the
# front chart component is source-agnostic (통일성).

def _distribution_response(d) -> DistributionResponse:
    breakdown = None
    if d.get("uncatalogued") is not None or d.get("ungenred") is not None:
        breakdown = UnclassifiedBreakdown(
            uncatalogued=d.get("uncatalogued") or 0,
            ungenred=d.get("ungenred") or 0,
        )
    return DistributionResponse(
        items=[DistItem(label=label, count=count) for label, count in d["items"]],
        unclassified_count=d["unclassified_count"],
        total=d["total"],
        unclassified_breakdown=breakdown,
        last_synced_at=d.get("last_synced_at"),
    )


@router.get("/saved-tracks", response_model=SavedTracksResponse)
def list_saved_tracks(
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows, total, last_synced = svc.list_saved_tracks(db, limit=limit, offset=offset)
    # Populate album.genres (tier-0 labels, [0] = primary) for rows whose album is in
    # our catalog — the /profile 분석 버킷 facet/chip groups by the primary genre.
    # Schema-identical (genres already on AlbumBrief): no contract change. Same
    # GenreService.labels_map the bucket view + the saved-tracks distribution use.
    album_ids = [str(r.album_id) for r in rows if r.album_id is not None]
    genre_map = GenreService().labels_map(db, album_ids) if album_ids else {}
    return SavedTracksResponse(
        items=[
            SavedTrackItem(
                spotify_track_id=r.spotify_track_id,
                track_id=str(r.track_id) if r.track_id else None,
                track_name=r.track_name,
                artist_name=r.artist_name,
                artist_id=_saved_track_artist_id(r),
                album_name=r.album_name,
                album_sid=r.album_sid,
                album_id=str(r.album_id) if r.album_id else None,
                album=_album_brief(r.album, genre_map.get(str(r.album_id), [])) if r.album is not None else None,
                added_at=r.added_at,
                duration_ms=r.duration_ms,
            )
            for r in rows
        ],
        total=total,
        last_synced_at=last_synced,
    )


@router.get("/saved-tracks/genre-distribution", response_model=DistributionResponse)
def saved_tracks_genre_distribution(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    return _distribution_response(svc.saved_tracks_genre_distribution(db))


@router.get("/saved-tracks/artist-distribution", response_model=DistributionResponse)
def saved_tracks_artist_distribution(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    return _distribution_response(svc.saved_tracks_artist_distribution(db))


@router.get("/play-events/genre-distribution", response_model=DistributionResponse)
def play_events_genre_distribution(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    return _distribution_response(svc.play_events_genre_distribution(db))


@router.get("/play-events/artist-distribution", response_model=DistributionResponse)
def play_events_artist_distribution(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    return _distribution_response(svc.play_events_artist_distribution(db))


# ── 분석 버킷: lifetime stream-history top tracks/artists (FEAT-listening-history-import) ──
# Ungated count/time rankings over the imported Spotify streaming history.
# FEAT-multi-user Phase 3 (library user-scope): every stream-history read is
# member-scoped (require_cognito_token via provisioned_member_id — was edge_guard)
# and returns ONLY the requesting member's imported history (strict user_id
# equality; the owner-poller live tail rides along for the owner only). GETs ride
# the API GW GET catch-all — the JWT is checked here at the Lambda, so no
# apigateway.tf change. `metric` flips the front Count↔Time axis;
# Literal makes FastAPI 422 on a bad value and self-documents in the contract.
# FEAT-analysis-explore: optional `from`/`to` (raw ISO timestamps; half-open [from, to))
# scope every panel to a time window. Omitted = lifetime+live (current behavior). `from`
# is a Python keyword → aliased; FastAPI parses ISO 8601 (tz-aware when an offset/Z is given).

_FROM = Query(None, alias="from", description="range start, inclusive (ISO 8601); omit for lifetime")
_TO = Query(None, alias="to", description="range end, exclusive (ISO 8601); omit for open-ended")


def _stream_rank_response(d) -> StreamRankResponse:
    return StreamRankResponse(
        items=[StreamRankItem(**it) for it in d["items"]],
        unit=d["unit"],
        unclassified=d.get("unclassified", 0),  # set by the gated genre/era panels; 0 for track/artist
        total_streams=d["total_streams"],
        total_ms=d["total_ms"],
        as_of=d["as_of"],
        live_streams=d.get("live_streams", 0),
    )


@router.get("/stream-history/top-tracks", response_model=StreamRankResponse)
def stream_history_top_tracks(
    metric: Literal["count", "time"] = "count",
    limit: int = 15,
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    limit = max(1, min(limit, 100))
    return _stream_rank_response(svc.stream_history_top_tracks(db, user_id=member_id, metric=metric, limit=limit, frm=from_, to=to))


@router.get("/stream-history/top-artists", response_model=StreamRankResponse)
def stream_history_top_artists(
    metric: Literal["count", "time"] = "count",
    limit: int = 15,
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    limit = max(1, min(limit, 100))
    return _stream_rank_response(svc.stream_history_top_artists(db, user_id=member_id, metric=metric, limit=limit, frm=from_, to=to))


# ── 분석 버킷: GATED lifetime panels — album / genre / era / retrospective (Step 5) ──
# Member-scoped JWT reads (see the Phase 3 note above). All need the catalog FK; the
# gate PASSED at 99.7% (Step 3), so they
# ship with the residual 미분류 carried as the `unclassified`/`unresolved` honesty caption.

def _stream_album_rank_response(d) -> StreamAlbumRankResponse:
    return StreamAlbumRankResponse(
        items=[
            StreamAlbumRankItem(album=_album_brief(it["album_obj"], it["genres"]), value=it["value"])
            for it in d["items"]
        ],
        unit=d["unit"],
        unresolved=d["unresolved"],
        total_streams=d["total_streams"],
        total_ms=d["total_ms"],
        as_of=d["as_of"],
        live_streams=d.get("live_streams", 0),
    )


@router.get("/stream-history/genre-distribution", response_model=StreamRankResponse)
def stream_history_genre_distribution(
    metric: Literal["count", "time"] = "count",
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    return _stream_rank_response(svc.stream_history_genre_distribution(db, user_id=member_id, metric=metric, frm=from_, to=to))


@router.get("/stream-history/era-distribution", response_model=StreamRankResponse)
def stream_history_era_distribution(
    metric: Literal["count", "time"] = "count",
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    return _stream_rank_response(svc.stream_history_era_distribution(db, user_id=member_id, metric=metric, frm=from_, to=to))


@router.get("/stream-history/top-albums", response_model=StreamAlbumRankResponse)
def stream_history_top_albums(
    metric: Literal["count", "time"] = "count",
    limit: int = 15,
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    limit = max(1, min(limit, 100))
    return _stream_album_rank_response(svc.stream_history_top_albums(db, user_id=member_id, metric=metric, limit=limit, frm=from_, to=to))


@router.get("/stream-history/retrospective", response_model=RetrospectiveResponse)
def stream_history_retrospective(
    limit: int = 20,
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    limit = max(1, min(limit, 100))
    d = svc.stream_history_retrospective(db, user_id=member_id, limit=limit, frm=from_, to=to)
    return RetrospectiveResponse(
        per_year=[RetroYearStat(**y) for y in d["per_year"]],
        on_this_day=[
            OnThisDayItem(
                year=it["year"],
                track_name=it["track_name"],
                artist_name=it["artist_name"],
                album_id=it["album_id"],
                album=_album_brief(it["album_obj"], it["genres"]) if it["album_obj"] is not None else None,
                plays=it["plays"],
                ms_played=it["ms_played"],
            )
            for it in d["on_this_day"]
        ],
        today_kst=d["today_kst"],
        as_of=d["as_of"],
        live_streams=d.get("live_streams", 0),
    )


# ── 분석 버킷: item drill-down + listening clock (FEAT-analysis-explore) ──────────────
# Member-scoped JWT reads (see the Phase 3 note above). Both honour the same
# optional [from, to) range. The drill-down keys an entity (artist | catalog album | track);
# `type` is a Literal so FastAPI 422s a bad value. The clock returns a KST hour×weekday matrix.

@router.get("/stream-history/item", response_model=StreamItemDetailResponse)
def stream_history_item(
    type: Literal["artist", "album", "track"],
    id: str,
    metric: Literal["count", "time"] = "count",
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        d = svc.stream_history_item_detail(db, user_id=member_id, type_=type, id_=id, metric=metric, frm=from_, to=to)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    return StreamItemDetailResponse(
        type=d["type"],
        id=d["id"],
        label=d["label"],
        artist=d["artist"],
        unit=d["unit"],
        count=d["count"],
        time_ms=d["time_ms"],
        first_listen=d["first_listen"],
        last_listen=d["last_listen"],
        per_year=[RetroYearStat(**y) for y in d["per_year"]],
        top_tracks=[StreamRankItem(**t) for t in d["top_tracks"]],
        top_albums=[
            StreamAlbumRankItem(album=_album_brief(it["album_obj"], it["genres"]), value=it["value"])
            for it in d["top_albums"]
        ],
        as_of=d["as_of"],
        live_streams=d["live_streams"],
    )


@router.get("/stream-history/clock", response_model=StreamClockResponse)
def stream_history_clock(
    metric: Literal["count", "time"] = "count",
    from_: Optional[datetime] = _FROM,
    to: Optional[datetime] = _TO,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    d = svc.stream_history_clock(db, user_id=member_id, metric=metric, frm=from_, to=to)
    return StreamClockResponse(
        cells=[StreamClockCell(**c) for c in d["cells"]],
        unit=d["unit"],
        total_streams=d["total_streams"],
        total_ms=d["total_ms"],
        as_of=d["as_of"],
        live_streams=d.get("live_streams", 0),
    )


@router.post("/saved-tracks/classify", response_model=ClassifyResponse, status_code=202)
def classify_saved_tracks(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    sqs=Depends(get_sqs_client),
    _claims: Dict = Depends(require_owner),
):
    # 분류하기 (owner action, JWT): enqueue the catalog-absent unclassified albums for
    # catalog sync (→ S1 genres → the track inherits a genre). Best-effort SQS only —
    # the worker does the Spotify read (rule #9). Catalog-present-but-ungenred tracks
    # need the iTunes backfill and are reported (skipped_needs_backfill), not enqueued.
    sids, needs_backfill = svc.unclassified_saved_album_targets(db)
    enqueued = safe_enqueue_catalog_sync(sqs, sids)
    return ClassifyResponse(
        enqueued=enqueued, skipped_needs_backfill=needs_backfill, status="queued"
    )


@router.post("/saved-tracks/fill-genres", response_model=FillGenresResponse, status_code=202)
def fill_saved_track_genres(
    db: Session = Depends(get_db),
    _claims: Dict = Depends(require_owner),
):
    # 장르 채우기 (owner action, JWT): enqueue an on-demand genre-backfill request. A
    # local 5-min poller claims it and runs the EXISTING backfill_genres.py
    # --incremental --label --execute (S1 mapping → S2 iTunes → S3 LLM) — the same
    # pipeline as the daily 04:00 launchd, just now. Deduped to one pending request.
    # Rule #9: only enqueues; the LLM (claude -p) runs on the owner's laptop via the
    # poller, never in this request.
    inserted = db.execute(
        text(
            """
            INSERT INTO genre_backfill_requests (source)
            SELECT '분석 버킷'
            WHERE NOT EXISTS (
                SELECT 1 FROM genre_backfill_requests WHERE claimed_at IS NULL
            )
            """
        )
    )
    db.commit()
    return FillGenresResponse(status="queued" if inserted.rowcount else "already_pending")


# ── to-listen mutations (Cognito JWT) ───────────────────────────────────────────
# /to-listen/reorder is declared before /to-listen/{item_id} so the literal path
# is unambiguous.

@router.put("/to-listen/reorder", status_code=204)
def reorder_to_listen(
    req: ToListenReorderRequest,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        svc.reorder_to_listen(db, member_id, req.item_ids)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)


@router.post(
    "/to-listen",
    response_model=ToListenItemResponse,
    status_code=201,
    responses={409: {"description": "Album already in the to-listen queue"}},
)
def add_to_listen(
    req: AddToListenRequest,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        item = svc.add_to_listen(db, member_id, album_id=req.album_id, note=req.note)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except DuplicateItemError:
        raise HTTPException(
            status_code=409, detail="Album already in the to-listen queue"
        )
    return _to_listen_response(item)


@router.delete("/to-listen/{item_id}", status_code=204)
def delete_to_listen(
    item_id: str,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    if not svc.delete_to_listen(db, member_id, item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)


# ── 최근 들은 앨범: manual "지금 새로고침" (Cognito JWT) ──────────────────────────────
# Pushes an async SQS job; the worker does the Spotify read (rule #9 — never sync).

@router.post("/refresh-recent", response_model=RefreshRecentResponse, status_code=202)
def refresh_recent(
    _claims: Dict = Depends(require_owner),
    sqs=Depends(get_sqs_client),
):
    sqs.send_listening_refresh()
    return RefreshRecentResponse(status="queued")
