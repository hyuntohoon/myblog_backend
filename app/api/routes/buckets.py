# app/api/routes/buckets.py
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    AddBucketItemRequest,
    AlbumBrief,
    BucketItemResponse,
    BucketResponse,
    BucketsResponse,
    CreateBucketRequest,
    MoveBucketRequest,
    PublicAlbumBrief,
    PublicBucket,
    PublicBucketItem,
    PublicBucketsResponse,
    ReorderRequest,
    SpotifyLibraryAlbumState,
    SpotifyLibraryStateResponse,
    SpotifyLibrarySyncResponse,
    UpdateBucketItemRequest,
    UpdateBucketRequest,
)
from app.clients.sqs_client import get_spotify_connection_status
from app.core.auth import require_cognito_token, resolve_owner
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
    BucketNotFoundError,
    BucketService,
    DuplicateItemError,
    ItemNotFoundError,
    TypedMembershipNotEnabledError,
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
    return BucketItemResponse(
        id=str(item.id),
        item_type=item_type,
        album_id=str(item.album_id) if item.album_id is not None else None,
        track_id=str(track_id) if track_id is not None else None,
        review_target_id=str(review_target_id) if review_target_id is not None else None,
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


# ── owner's full board (Cognito JWT) ─────────────────────────────────────────────
# FEAT-public-bucket-multiuser Scope A / A5: this returns the owner's ENTIRE bucket
# tree incl. private/workflow crates + the spotify_library bucket, so it is now
# Cognito-gated (was edge_guard-only — every front caller already sends Bearer via
# apiFetch). Unauthenticated read of public shelves lives at GET /public below.

@router.get("", response_model=BucketsResponse)
def list_buckets(
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    genre_svc: GenreService = Depends(get_genre_service),
    _claims: Dict = Depends(require_cognito_token),
):
    roots = svc.list_buckets(db)
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
    buckets = svc.list_public_buckets(db)
    # The public viewer projects album shelves only; a non-album row (album_id NULL, post
    # STEP-2) is simply not shown rather than 500ing the unauthenticated viewer.
    all_album_ids = [
        str(it.album_id) for b in buckets for it in b.items if it.album_id is not None
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
            for b in buckets
        ]
    )


# ── Spotify Library sync (FEAT-spotify-library-sync) ────────────────────────────
# The single kind='spotify_library' bucket mirrors the owner's Spotify saved albums.
# State GET rides the edge_guard catch-all (no apigateway route); the sync POST is
# Cognito-JWT (matching apigateway route added in infra/apigateway.tf). Declared with
# literal /spotify-library/* paths (unambiguous vs the /{bucket_id} patterns below).


@router.get("/spotify-library/state", response_model=SpotifyLibraryStateResponse)
def spotify_library_state(
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
):
    # Pure read of the worker-written spotify_library_albums table (rule #9 — no
    # Spotify call). The bucket is NOT created here (only the sync POST get-or-creates).
    bucket, last_synced_at, albums = svc.get_spotify_library_state(db)
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
    _claims: Dict = Depends(require_cognito_token),
):
    # Get-or-create the special bucket so the worker always has a destination, then
    # enqueue the async job. Rule #9: this only ENQUEUES — never calls Spotify; the
    # worker does the reads/diffs/writes. Server-side debounce makes a rapid re-tap
    # a no-op (status="debounced") without hitting the queue again.
    svc.get_or_create_spotify_library_bucket(db)
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
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        bucket = svc.create_bucket(db, name=req.name, color=req.color)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return BucketResponse(
        id=str(bucket.id),
        name=bucket.name,
        position=bucket.position,
        color=bucket.color,
        is_done=bucket.is_done,
        kind=bucket.kind,
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
    _claims: Dict = Depends(require_cognito_token),
):
    updates = req.model_dump(exclude_unset=True)
    try:
        bucket = svc.update_bucket(db, bucket_id, **updates)
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
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


@router.delete("/{bucket_id}", status_code=204)
def delete_bucket(
    bucket_id: str,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    _claims: Dict = Depends(require_cognito_token),
):
    if not svc.delete_bucket(db, bucket_id):
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
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        svc.reorder(db, [b.model_dump() for b in req.buckets])
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    # Auto-research: dragging an album INTO an 'all'-mode bucket persists through
    # this cross-bucket reorder (bucket_id reassigned), which add_item's enqueue
    # never sees — so a moved-in album would otherwise never get a research row.
    # Enqueue each touched 'all' bucket's note-less items here too (dedup-gated, so
    # already-noted/queued albums and pure within-bucket reorders are no-ops).
    for b in req.buckets:
        bucket = svc.get_bucket(db, b.id)
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
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        svc.move_bucket(db, bucket_id, parent_id=req.parent_id, position=req.position)
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Return the full updated nested tree (same shape as GET /api/buckets).
    return list_buckets(
        db=db, svc=svc, research_svc=research_svc, genre_svc=genre_svc
    )


# ── item operations (Cognito JWT) ───────────────────────────────────────────────

@router.post(
    "/{bucket_id}/items",
    response_model=BucketItemResponse,
    status_code=201,
    responses={409: {"description": "Album already in this bucket"}},
)
def add_item(
    bucket_id: str,
    req: AddBucketItemRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    research_svc: ResearchService = Depends(get_research_service),
    genre_svc: GenreService = Depends(get_genre_service),
    claims: Dict = Depends(require_cognito_token),
):
    # FEAT-pocket-buckit Step 3 (OQ11): the drop endpoint reads the owner from the verified
    # JWT `sub`, never the request body (single-owner today; the public-page sign-in handoff
    # in Step 5 replays its pending intent against this route). No owner_id is stored in v1
    # (OQ12) — this establishes the first-`sub`-consumer pattern + the local/dev fallback.
    _owner = resolve_owner(claims)
    try:
        item = svc.add_item(
            db, bucket_id, album_id=req.album_id, note=req.note, item_type=req.item_type
        )
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except TypedMembershipNotEnabledError:
        # Non-album membership writes land with the STEP-2 relax (Step 6). Clean 422 instead
        # of a leaked IntegrityError on album_id NOT NULL.
        raise HTTPException(
            status_code=422,
            detail="Only album items can be added yet (typed membership pending schema STEP-2)",
        )
    except DuplicateItemError:
        raise HTTPException(status_code=409, detail="Album already in this bucket")
    reviewed = svc.reviewed_album_ids(db, [str(item.album_id)])
    research = research_svc.status_map(db, [str(item.album_id)])
    genres = genre_svc.labels_map(db, [str(item.album_id)])
    resp = _item_response(
        item,
        str(item.album_id) in reviewed,
        research.get(str(item.album_id)),
        genres.get(str(item.album_id)),
    )
    # Auto-research: an item added to an 'all'-mode bucket is enqueued (dedup-gated).
    bucket = svc.get_bucket(db, bucket_id)
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
    _claims: Dict = Depends(require_cognito_token),
):
    updates = req.model_dump(exclude_unset=True)
    try:
        item = svc.update_item(db, bucket_id, item_id, **updates)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    reviewed = svc.reviewed_album_ids(db, [str(item.album_id)])
    research = research_svc.status_map(db, [str(item.album_id)])
    genres = genre_svc.labels_map(db, [str(item.album_id)])
    resp = _item_response(
        item,
        str(item.album_id) in reviewed,
        research.get(str(item.album_id)),
        genres.get(str(item.album_id)),
    )
    # Auto-research: checking research_selected while the bucket is 'selected' mode
    # enqueues that album (dedup-gated). Unchecking/other updates trigger nothing.
    if updates.get("research_selected") is True:
        bucket = svc.get_bucket(db, bucket_id)
        if bucket is not None and bucket.research_mode == "selected":
            _safe_enqueue_album(db, research_svc, item.album_id)
    return resp


@router.delete("/{bucket_id}/items/{item_id}", status_code=204)
def delete_item(
    bucket_id: str,
    item_id: str,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    _claims: Dict = Depends(require_cognito_token),
):
    if not svc.delete_item(db, bucket_id, item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)
