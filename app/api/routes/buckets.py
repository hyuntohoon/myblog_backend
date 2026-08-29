# app/api/routes/buckets.py
import logging
import uuid
from typing import Dict, Union

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    AddBucketItemRequest,
    AlbumBrief,
    ArtistBrief,
    ArtistExpansionResponse,
    BucketItemExpansion,
    BucketItemResponse,
    BucketResponse,
    BucketsResponse,
    CreateBucketRequest,
    MoveBucketRequest,
    NightlyGrowRequest,
    NightlyGrowResponse,
    PublicAlbumBrief,
    PublicBucket,
    PublicBucketItem,
    PublicBucketOwner,
    PublicBucketsResponse,
    ReorderRequest,
    SpotifyLibraryAlbumState,
    SpotifyLibraryStateResponse,
    SpotifyLibrarySyncResponse,
    TrackBrief,
    TrackExpansion,
    TrackExpansionResponse,
    UpdateBucketItemRequest,
    UpdateBucketRequest,
)
from app.api.routes.me import provisioned_member_id, provisioned_owner_id
from app.clients.sqs_client import get_spotify_connection_status
from app.core.authz import require_owner_or_draft_agent
from app.core.config import get_settings
from app.db.session import get_db
from app.di import (
    get_bucket_service,
    get_genre_service,
    get_research_service,
    get_sqs_client,
)
from app.services.bucket_service import (
    AlbumNotFoundError,
    ArtistNotFoundError,
    BucketNotFoundError,
    BucketItemRateLimitError,
    BucketRateLimitError,
    BucketService,
    BucketTypeError,
    DuplicateItemError,
    GrowPostNotDraftError,
    GrowPostNotFoundError,
    ItemNotFoundError,
    ReviewTargetNotFoundError,
    SystemBucketError,
    TrackNotFoundError,
)
from app.services.enqueue import (
    safe_enqueue_album as _safe_enqueue_album,
    safe_enqueue_bucket as _safe_enqueue_bucket,
)
from app.services.genre_service import GenreService
from app.services.research_service import ResearchService

logger = logging.getLogger(__name__)

router = APIRouter()


# Auto-research enqueue (FEAT-album-research-notes) now lives in
# app/services/enqueue.py (통일성: the 분석 버킷 분류하기 shares that fire-and-forget
# module). Imported above as _safe_enqueue_album / _safe_enqueue_bucket — fire-and-
# forget, $0 INSERT, a research hiccup never fails the bucket op; call sites unchanged.


def _album_brief(album, genres: list[str] | None = None) -> AlbumBrief:
    return AlbumBrief(
        id=str(album.id),
        title=album.title,
        cover_url=album.cover_url,
        release_date=album.release_date,
        popularity=album.popularity,
        artist_names=[a.name for a in album.artists],
        genres=genres or [],
    )


def _track_brief(track) -> TrackBrief:
    album = getattr(track, "album", None)
    return TrackBrief(
        id=str(track.id),
        title=track.title,
        album_id=str(track.album_id) if getattr(track, "album_id", None) is not None else None,
        cover_url=album.cover_url if album is not None else None,
        artist_names=[a.name for a in track.artists],
        duration_sec=getattr(track, "duration_sec", None),
    )


def _artist_brief(artist) -> ArtistBrief:
    # FEAT-my-buckit-artist (V32): minimal artist display for an artist-kind membership row and
    # for the expansion added/skipped lists. Member click-through → /artist/[id] (front).
    return ArtistBrief(
        id=str(artist.id),
        name=artist.name,
        photo_url=getattr(artist, "photo_url", None),
    )


def _item_response(
    item,
    already_reviewed: bool,
    research_status: str | None = None,
    genres: list[str] | None = None,
) -> BucketItemResponse:
    # FEAT-pocket-buckit Step 3 — non-album-TOLERANT serializer. The pre-Step-3 code did
    # `str(item.album_id)` + `_album_brief(item.album, …)` UNCONDITIONALLY; on a non-album
    # row (album_id NULL, no `.album`) that 500s the whole board. Branch on the typed
    # membership: emit the AlbumBrief only when an album is present, and null the
    # album fields otherwise. Ships + is prod-verified BEFORE the STEP-2 relax that lets
    # non-album rows exist, so the first such row can't 500 GET /api/buckets.
    item_type = getattr(item, "item_type", "album")
    if not isinstance(item_type, str):  # defensive: ORM None / a test MagicMock → 'album'
        item_type = "album"
    album = getattr(item, "album", None)
    track_id = getattr(item, "track_id", None)
    review_target_id = getattr(item, "review_target_id", None)
    artist_id = getattr(item, "artist_id", None)
    # Track display for track/playback rows (gated on item_type so an album row's
    # auto-vivified ORM `.track` is never touched).
    track = getattr(item, "track", None) if item_type in ("track", "playback") else None
    # FEAT-my-buckit-artist (V32): artist display for an artist row (gated on item_type so a
    # non-artist row's auto-vivified ORM `.artist` is never touched).
    artist = getattr(item, "artist", None) if item_type == "artist" else None
    return BucketItemResponse(
        id=str(item.id),
        item_type=item_type,
        album_id=str(item.album_id) if item.album_id is not None else None,
        track_id=str(track_id) if track_id is not None else None,
        review_target_id=str(review_target_id) if review_target_id is not None else None,
        artist_id=str(artist_id) if artist_id is not None else None,
        position=item.position,
        note=item.note,
        status=item.status,
        post_id=str(item.post_id) if item.post_id else None,
        rec_reason=item.rec_reason,
        already_reviewed=already_reviewed,
        research_selected=item.research_selected,
        research_status=research_status,
        prep_tonight=item.prep_tonight,
        album=_album_brief(album, genres) if album is not None else None,
        track=_track_brief(track) if track is not None else None,
        artist=_artist_brief(artist) if artist is not None else None,
    )


# ── tree serialization ──────────────────────────────────────────────────────
# list_buckets() returns root ReviewBucket objects, each carrying its descendants
# on a transient `children_nodes` list. Serialize recursively into nested
# BucketResponse, sharing one already_reviewed lookup across the whole tree.

def _iter_tree(roots):
    """Depth-first yield of every node in the forest (roots + descendants)."""
    for node in roots:
        yield node
        yield from _iter_tree(getattr(node, "children_nodes", []) or [])


def _bucket_response(
    b, reviewed: set, research: dict[str, str], genres: dict[str, list[str]]
) -> BucketResponse:
    return BucketResponse(
        id=str(b.id),
        name=b.name,
        position=b.position,
        color=b.color,
        is_done=b.is_done,
        kind=b.kind,
        type=b.type,
        research_mode=b.research_mode,
        is_public=b.is_public,
        items=[
            _item_response(
                it,
                # Non-album rows have no album_id → not "reviewed", no research/genre map.
                str(it.album_id) in reviewed if it.album_id is not None else False,
                research.get(str(it.album_id)) if it.album_id is not None else None,
                genres.get(str(it.album_id)) if it.album_id is not None else None,
            )
            for it in b.items
        ],
        children=[
            _bucket_response(child, reviewed, research, genres)
            for child in (getattr(b, "children_nodes", []) or [])
        ],
    )


# ── the caller's full board (Cognito JWT, per-user) ──────────────────────────────
# FEAT-multi-user Phase 2: returns the AUTHENTICATED MEMBER's ENTIRE bucket tree
# incl. their private/workflow crates + (for the owner) the spotify_library bucket,
# scoped to their user_id. Was require_owner (single-user); now any member gets
# their own board. Unauthenticated read of public shelves lives at GET /public below.

@router.get("", response_model=BucketsResponse)
def list_buckets(
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    genre_svc: GenreService = Depends(get_genre_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    # FEAT-playback-bucket-player: the Playback Bucket is created lazily, here, on the member's
    # first bucket-tree read — not by a migration backfill. Idempotent, and it must run BEFORE
    # list_buckets so a first-time member gets the queue in the same response rather than only
    # on their second load. Eligibility gates PLAYING, not EXISTING (T1), so there is no Spotify
    # check here — every member gets the bucket.
    svc.get_or_create_playback_bucket(db, member_id)
    roots = svc.list_buckets(db, member_id)
    # Batch the already_reviewed + research-status + genre-label lookups across every
    # album in the whole tree (one query each), then serialize roots recursively.
    # Skip non-album rows (album_id NULL) — they carry no album to look up.
    all_album_ids = [
        str(it.album_id)
        for b in _iter_tree(roots)
        for it in b.items
        if it.album_id is not None
    ]
    reviewed = svc.reviewed_album_ids(db, all_album_ids)
    research = research_svc.status_map(db, all_album_ids)
    genres = genre_svc.labels_map(db, all_album_ids)
    return BucketsResponse(
        buckets=[
            _bucket_response(root, reviewed, research, genres) for root in roots
        ]
    )


# ── public read-only viewer (NO auth — FEAT-public-bucket-multiuser Scope A) ─────
# Whitelisted projection of is_public=true, kind='review' buckets ONLY, served
# unauthenticated (edge_guard catch-all) so the public /collection viewer renders
# without login. Private item fields are dropped by the Public* schemas; the
# spotify_library bucket is excluded in the service; flat (no nesting) so the
# private tree structure can't leak. Declared before the /{bucket_id}/* patterns;
# /public is a literal GET with no GET /{bucket_id} sibling, so no ambiguity.

@router.get("/public", response_model=PublicBucketsResponse)
def list_public_buckets(
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    genre_svc: GenreService = Depends(get_genre_service),
):
    rows = svc.list_public_buckets(db)
    # The public viewer projects album shelves only; a non-album row (album_id NULL, post
    # STEP-2) is simply not shown rather than 500ing the unauthenticated viewer.
    all_album_ids = [
        str(it.album_id) for b, _ in rows for it in b.items if it.album_id is not None
    ]
    reviewed = svc.reviewed_album_ids(db, all_album_ids)
    genres = genre_svc.labels_map(db, all_album_ids)
    return PublicBucketsResponse(
        buckets=[
            PublicBucket(
                id=str(b.id),
                name=b.name,
                position=b.position,
                color=b.color,
                # Attribution: post-P2 any member can publish a bucket — the public
                # viewer must say whose shelf this is (never anonymous).
                owner=PublicBucketOwner(
                    handle=owner.handle,
                    display_name=owner.display_name,
                ),
                items=[
                    PublicBucketItem(
                        album_id=str(it.album_id),
                        position=it.position,
                        already_reviewed=str(it.album_id) in reviewed,
                        album=PublicAlbumBrief(
                            id=str(it.album.id),
                            title=it.album.title,
                            cover_url=it.album.cover_url,
                            release_date=it.album.release_date,
                            artist_names=[a.name for a in it.album.artists],
                            genres=genres.get(str(it.album_id), []),
                        ),
                    )
                    for it in b.items
                    if it.album_id is not None
                ],
            )
            for b, owner in rows
        ]
    )


# ── Spotify Library sync (FEAT-spotify-library-sync) ────────────────────────────
# The single kind='spotify_library' bucket mirrors the owner's Spotify saved albums.
# Both the state GET and the sync POST are Cognito-JWT, owner-only
# (provisioned_owner_id), with matching apigateway routes in infra/apigateway.tf
# (BUG-25 fixed the GET's missing auth + missing route). Declared with literal
# /spotify-library/* paths (unambiguous vs the /{bucket_id} patterns below).


@router.get("/spotify-library/state", response_model=SpotifyLibraryStateResponse)
def spotify_library_state(
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    owner_id: uuid.UUID = Depends(provisioned_owner_id),
):
    # BUG-25: this GET had no auth dependency at all (unlike the sibling POST
    # below), so it rode the unauthenticated api_get_proxy catch-all — anyone
    # could read the owner's synced Spotify library state without logging in.
    # Matches the POST's tier (provisioned_owner_id): the Spotify lane is
    # owner-only until Phase 3b, same as get_or_create_spotify_library_bucket.
    #
    # Pure read of the worker-written spotify_library_albums table (rule #9 — no
    # Spotify call). The bucket is NOT created here (only the sync POST get-or-creates).
    bucket, last_synced_at, albums = svc.get_spotify_library_state(db, owner_id)
    conn = get_spotify_connection_status()
    return SpotifyLibraryStateResponse(
        bucket_id=str(bucket.id) if bucket is not None else None,
        last_synced_at=last_synced_at,
        needs_reauth=conn.needs_reauth,
        # Read-only mirror of the worker write gate, for the "검토 모드" banner.
        writes_enabled=get_settings().SPOTIFY_LIBRARY_WRITES_ENABLED,
        albums=[
            SpotifyLibraryAlbumState(
                album_id=str(row.album_id),
                spotify_id=row.spotify_id,
                source=row.source,
                state=row.state,
                in_bucket=row.in_bucket,
                in_spotify=row.in_spotify,
                last_error=row.last_error,
            )
            for row in albums
        ],
    )


@router.post(
    "/spotify-library/sync",
    response_model=SpotifyLibrarySyncResponse,
    status_code=202,
)
def spotify_library_sync(
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    sqs=Depends(get_sqs_client),
    owner_id: uuid.UUID = Depends(provisioned_owner_id),
):
    # Get-or-create the special bucket so the worker always has a destination, then
    # enqueue the async job. Rule #9: this only ENQUEUES — never calls Spotify; the
    # worker does the reads/diffs/writes. Server-side debounce makes a rapid re-tap
    # a no-op (status="debounced") without hitting the queue again. The bucket carries
    # user_id = owner (Spotify lane owner-only until Phase 3b).
    svc.get_or_create_spotify_library_bucket(db, owner_id)
    if svc.library_sync_debounced(db):
        logger.info("spotify library sync debounced (recent sync within window)")
        return SpotifyLibrarySyncResponse(status="debounced")
    sqs.send_library_sync()
    return SpotifyLibrarySyncResponse(status="queued")


# ── bucket CRUD (Cognito JWT) ───────────────────────────────────────────────────

@router.post("", response_model=BucketResponse, status_code=201)
def create_bucket(
    req: CreateBucketRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        bucket = svc.create_bucket(
            db,
            member_id,
            name=req.name,
            color=req.color,
            type=req.type,
            daily_cap=get_settings().BUCKET_DAILY_CAP,
        )
    except BucketRateLimitError:
        raise HTTPException(
            status_code=429,
            detail="Daily bucket creation limit reached — try again later",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return BucketResponse(
        id=str(bucket.id),
        name=bucket.name,
        position=bucket.position,
        color=bucket.color,
        is_done=bucket.is_done,
        kind=bucket.kind,
        type=bucket.type,
        research_mode=bucket.research_mode,
        is_public=bucket.is_public,
        items=[],
    )


@router.patch("/{bucket_id}", response_model=BucketResponse)
def update_bucket(
    bucket_id: str,
    req: UpdateBucketRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    genre_svc: GenreService = Depends(get_genre_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    updates = req.model_dump(exclude_unset=True)
    try:
        bucket = svc.update_bucket(db, member_id, bucket_id, **updates)
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except BucketTypeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        # Partial unique index: only one is_done bucket allowed.
        db.rollback()
        raise HTTPException(
            status_code=409, detail='"평론 완료" 버킷은 하나만 지정할 수 있습니다.'
        )
    album_ids = [str(it.album_id) for it in bucket.items if it.album_id is not None]
    reviewed = svc.reviewed_album_ids(db, album_ids)
    research = research_svc.status_map(db, album_ids)
    genres = genre_svc.labels_map(db, album_ids)
    resp = BucketResponse(
        id=str(bucket.id),
        name=bucket.name,
        position=bucket.position,
        color=bucket.color,
        is_done=bucket.is_done,
        kind=bucket.kind,
        type=bucket.type,
        research_mode=bucket.research_mode,
        is_public=bucket.is_public,
        items=[
            _item_response(
                it,
                str(it.album_id) in reviewed if it.album_id is not None else False,
                research.get(str(it.album_id)) if it.album_id is not None else None,
                genres.get(str(it.album_id)) if it.album_id is not None else None,
            )
            for it in bucket.items
        ],
    )
    # Auto-research: switching a bucket to 'all'/'selected' enqueues its note-less
    # in-scope items (dedup-gated; flipping modes never re-calls noted albums).
    if updates.get("research_mode") in ("all", "selected"):
        _safe_enqueue_bucket(db, research_svc, bucket)
    return resp


@router.delete(
    "/{bucket_id}",
    status_code=204,
    responses={409: {"description": "System bucket — cannot be deleted"}},
)
def delete_bucket(
    bucket_id: str,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        deleted = svc.delete_bucket(db, member_id, bucket_id)
    except SystemBucketError as e:
        # FEAT-playback-bucket-player Step 3: 409, not 403 — the caller DOES own the bucket
        # (404 already covers "not yours"); the request conflicts with the bucket's
        # system-owned state. Covers playback_queue / spotify_library / to_listen alike.
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Bucket not found")
    return Response(status_code=204)


# ── drag-and-drop persistence (Cognito JWT) ─────────────────────────────────────
# Declared before /{bucket_id}/items so the literal /reorder path is unambiguous.

@router.put("/reorder", status_code=204)
def reorder(
    req: ReorderRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        svc.reorder(db, member_id, [b.model_dump() for b in req.buckets])
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    except BucketTypeError as e:
        # FEAT-my-buckit-artist (V32): a cross-bucket move of a non-artist item into an Artist
        # bucket is rejected (the move path's artist-only gate).
        raise HTTPException(status_code=400, detail=str(e))
    except SystemBucketError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # Auto-research: dragging an album INTO an 'all'-mode bucket persists through
    # this cross-bucket reorder (bucket_id reassigned), which add_item's enqueue
    # never sees — so a moved-in album would otherwise never get a research row.
    # Enqueue each touched 'all' bucket's note-less items here too (dedup-gated, so
    # already-noted/queued albums and pure within-bucket reorders are no-ops).
    for b in req.buckets:
        bucket = svc.get_bucket(db, b.id, member_id)
        if bucket is not None and bucket.research_mode == "all":
            _safe_enqueue_bucket(db, research_svc, bucket)
    return Response(status_code=204)


# ── tree movement (Cognito JWT) ─────────────────────────────────────────────────
# FEAT-member-dashboard Step 5: reparent + reposition. Path /{bucket_id}/move is
# unambiguous (move is a literal suffix), so ordering vs /{bucket_id} is fine.

@router.put("/{bucket_id}/move", response_model=BucketsResponse)
def move_bucket(
    bucket_id: str,
    req: MoveBucketRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    genre_svc: GenreService = Depends(get_genre_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        svc.move_bucket(
            db, bucket_id, parent_id=req.parent_id, position=req.position, user_id=member_id
        )
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Return the full updated nested tree (same shape as GET /api/buckets).
    return list_buckets(
        db=db, svc=svc, research_svc=research_svc, genre_svc=genre_svc, member_id=member_id
    )


# ── item operations (Cognito JWT) ───────────────────────────────────────────────

@router.post(
    "/{bucket_id}/items",
    response_model=Union[BucketItemResponse, ArtistExpansionResponse, TrackExpansionResponse],
    status_code=201,
    responses={409: {"description": "Item already in this bucket"}},
)
def add_item(
    bucket_id: str,
    req: AddBucketItemRequest,
    response: Response,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    genre_svc: GenreService = Depends(get_genre_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    # FEAT-multi-user Phase 2: the drop endpoint scopes to the authenticated member;
    # the item lands in the member's own (owned) bucket. The public-page sign-in
    # handoff (Step 5) replays its pending intent against this route after login.

    # FEAT-my-buckit-artist (V32): a source_* artist add EXPANDS the source (featuring track /
    # compilation album) into its credited artists. The source row is never stored; the result
    # is the added/skipped lists, returned as an expansion SUMMARY (no single membership row to
    # echo, so id/position/status are null). 201 when ≥1 artist was added, 200 on a pure no-op
    # (VA compilation → 0, or every credited artist already present).
    if req.item_type == "artist" and (req.source_album_id or req.source_track_id):
        try:
            added, skipped = svc.expand_artist_source(
                db,
                member_id,
                bucket_id,
                source_album_id=req.source_album_id,
                source_track_id=req.source_track_id,
                daily_cap=get_settings().BUCKET_ITEM_DAILY_CAP,
            )
        except BucketNotFoundError:
            raise HTTPException(status_code=404, detail="Bucket not found")
        except AlbumNotFoundError:
            raise HTTPException(status_code=404, detail="Album not found")
        except TrackNotFoundError:
            raise HTTPException(status_code=404, detail="Track not found")
        except BucketItemRateLimitError:
            raise HTTPException(
                status_code=429,
                detail="Daily bucket item limit reached — try again later",
            )
        except SystemBucketError as e:
            # Same convention as delete_bucket: 409, the caller owns the bucket but the
            # request conflicts with its system-owned (sync-only) state.
            raise HTTPException(status_code=409, detail=str(e))
        response.status_code = 201 if added else 200
        return ArtistExpansionResponse(
            expansion=BucketItemExpansion(
                added=[_artist_brief(a) for a in added],
                skipped=[_artist_brief(a) for a in skipped],
            ),
        )

    # FEAT-playback-bucket-player: an ALBUM dropped on the Playback Bucket expands into its
    # tracks, in album order — the same source-expansion idiom as the artist branch above (one
    # POST /items, a source_* id, an expansion summary back), so it rides this existing route
    # and needs no new API Gateway entry. The album row itself is never stored; the single-row
    # album path into a playback bucket is a 400 from the service type gate.
    if req.item_type == "playback" and req.source_album_id:
        try:
            tracks = svc.expand_album_tracks(
                db,
                member_id,
                bucket_id,
                source_album_id=req.source_album_id,
                daily_cap=get_settings().BUCKET_ITEM_DAILY_CAP,
            )
        except BucketNotFoundError:
            raise HTTPException(status_code=404, detail="Bucket not found")
        except AlbumNotFoundError:
            raise HTTPException(status_code=404, detail="Album not found")
        except BucketTypeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except BucketItemRateLimitError:
            raise HTTPException(
                status_code=429,
                detail="Daily bucket item limit reached — try again later",
            )
        except SystemBucketError as e:
            raise HTTPException(status_code=409, detail=str(e))
        # 200 on a no-op (an album whose tracks were never synced → nothing queued),
        # 201 when rows were appended — the artist-expansion convention.
        response.status_code = 201 if tracks else 200
        return TrackExpansionResponse(
            expansion=TrackExpansion(added=[_track_brief(t) for t in tracks]),
        )

    try:
        item = svc.add_item(
            db,
            member_id,
            bucket_id,
            item_type=req.item_type,
            album_id=req.album_id,
            track_id=req.track_id,
            review_target_id=req.review_target_id,
            artist_id=req.artist_id,
            note=req.note,
            snapshot=req.snapshot,
            daily_cap=get_settings().BUCKET_ITEM_DAILY_CAP,
        )
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except BucketTypeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except TrackNotFoundError:
        raise HTTPException(status_code=404, detail="Track not found")
    except ReviewTargetNotFoundError:
        raise HTTPException(status_code=404, detail="Review target not found")
    except ArtistNotFoundError:
        raise HTTPException(status_code=404, detail="Artist not found")
    except DuplicateItemError:
        raise HTTPException(status_code=409, detail="Item already in this bucket")
    except BucketItemRateLimitError:
        raise HTTPException(
            status_code=429,
            detail="Daily bucket item limit reached — try again later",
        )
    except SystemBucketError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # Album-only enrichments — skip for non-album rows (album_id NULL) so the UUID-typed
    # .in_() lookups never receive a "None" string (the same guard as list_buckets/update_item).
    album_ids = [str(item.album_id)] if item.album_id is not None else []
    reviewed = svc.reviewed_album_ids(db, album_ids)
    research = research_svc.status_map(db, album_ids)
    genres = genre_svc.labels_map(db, album_ids)
    resp = _item_response(
        item,
        str(item.album_id) in reviewed if item.album_id is not None else False,
        research.get(str(item.album_id)) if item.album_id is not None else None,
        genres.get(str(item.album_id)) if item.album_id is not None else None,
    )
    # Auto-research: an album added to an 'all'-mode bucket is enqueued (dedup-gated). Album
    # rows only — a non-album row has no album to research.
    if item.album_id is not None:
        bucket = svc.get_bucket(db, bucket_id, member_id)
        if bucket is not None and bucket.research_mode == "all":
            _safe_enqueue_album(db, research_svc, item.album_id)
    return resp


@router.patch(
    "/{bucket_id}/items/{item_id}", response_model=BucketItemResponse
)
def update_item(
    bucket_id: str,
    item_id: str,
    req: UpdateBucketItemRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    genre_svc: GenreService = Depends(get_genre_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    updates = req.model_dump(exclude_unset=True)
    try:
        item = svc.update_item(db, member_id, bucket_id, item_id, **updates)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    # Non-album-tolerant (FEAT-pocket-buckit Step 3): update_item has NO item_type gate
    # (unlike add_item), so post-STEP-2 it can return a non-album row (album_id NULL). An
    # unconditional [str(item.album_id)] would become ["None"] → uuid.UUID("None") in the
    # UUID-typed .in_() lookups → 500. Guard the album-id batch like every other read path.
    album_ids = [str(item.album_id)] if item.album_id is not None else []
    reviewed = svc.reviewed_album_ids(db, album_ids)
    research = research_svc.status_map(db, album_ids)
    genres = genre_svc.labels_map(db, album_ids)
    resp = _item_response(
        item,
        str(item.album_id) in reviewed if item.album_id is not None else False,
        research.get(str(item.album_id)) if item.album_id is not None else None,
        genres.get(str(item.album_id)) if item.album_id is not None else None,
    )
    # Auto-research: checking research_selected while the bucket is 'selected' mode
    # enqueues that album (dedup-gated). Unchecking/other updates trigger nothing. Album
    # rows only — a non-album row has no album to research.
    if updates.get("research_selected") is True and item.album_id is not None:
        bucket = svc.get_bucket(db, bucket_id, member_id)
        if bucket is not None and bucket.research_mode == "selected":
            _safe_enqueue_album(db, research_svc, item.album_id)
    return resp


@router.delete("/{bucket_id}/items/{item_id}", status_code=204)
def delete_item(
    bucket_id: str,
    item_id: str,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    if not svc.delete_item(db, member_id, bucket_id, item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)


@router.post(
    "/nightly-grow",
    response_model=NightlyGrowResponse,
    responses={
        404: {"description": "post_id does not exist"},
        409: {"description": "post_id is not a draft"},
    },
)
def nightly_grow(
    req: NightlyGrowRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    _claims: Dict = Depends(require_owner_or_draft_agent),
):
    """FIX-nightly-draft-identity: grow-once for the 03:00 draft agent.

    After creating a draft the nightly job must mark the source memo processed
    (stamp post_id + clear prep_tonight) or the same album is regenerated every
    night and 409s forever. The generic item PATCH cannot do it — it is
    member-scoped and the agent owns no buckets (404 by design, per #133). This
    narrow route is the replacement: the caller names an album and the draft it
    created; the service touches ONLY the owner's checked memos for that album
    (owner pinned from settings, never the request body) and refuses to stamp
    anything but a draft. Idempotent — a repeat call returns grown=0.
    """
    settings = get_settings()
    try:
        grown = svc.grow_nightly(db, settings.OWNER_SUB, req.album_id, req.post_id)
    except GrowPostNotFoundError:
        raise HTTPException(status_code=404, detail="Post not found")
    except GrowPostNotDraftError:
        raise HTTPException(status_code=409, detail="Post is not a draft")
    return NightlyGrowResponse(grown=grown)
