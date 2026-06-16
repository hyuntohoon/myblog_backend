# app/api/routes/library.py
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.schemas import (
    AddToListenRequest,
    AlbumBrief,
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
    SpotifyConnectionResponse,
    ToListenItemResponse,
    ToListenReorderRequest,
    ToListenResponse,
)
from app.clients.sqs_client import get_spotify_connection_status
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_library_service, get_sqs_client
from app.services.library_service import (
    AlbumNotFoundError,
    DuplicateItemError,
    ItemNotFoundError,
    LibraryService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _album_brief(album) -> AlbumBrief:
    return AlbumBrief(
        id=str(album.id),
        title=album.title,
        cover_url=album.cover_url,
        release_date=album.release_date,
        popularity=album.popularity,
        artist_names=[a.name for a in album.artists],
    )


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

@router.get("/to-listen", response_model=ToListenResponse)
def list_to_listen(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    items = svc.list_to_listen(db)
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


# ── to-listen mutations (Cognito JWT) ───────────────────────────────────────────
# /to-listen/reorder is declared before /to-listen/{item_id} so the literal path
# is unambiguous.

@router.put("/to-listen/reorder", status_code=204)
def reorder_to_listen(
    req: ToListenReorderRequest,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        svc.reorder_to_listen(db, req.item_ids)
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
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        item = svc.add_to_listen(db, album_id=req.album_id, note=req.note)
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
    _claims: Dict = Depends(require_cognito_token),
):
    if not svc.delete_to_listen(db, item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)


# ── 최근 들은 앨범: manual "지금 새로고침" (Cognito JWT) ──────────────────────────────
# Pushes an async SQS job; the worker does the Spotify read (rule #9 — never sync).

@router.post("/refresh-recent", response_model=RefreshRecentResponse, status_code=202)
def refresh_recent(
    _claims: Dict = Depends(require_cognito_token),
    sqs=Depends(get_sqs_client),
):
    sqs.send_listening_refresh()
    return RefreshRecentResponse(status="queued")
