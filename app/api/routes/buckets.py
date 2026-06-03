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
    ReorderRequest,
    UpdateBucketItemRequest,
    UpdateBucketRequest,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_bucket_service
from app.services.bucket_service import (
    AlbumNotFoundError,
    BucketNotFoundError,
    BucketService,
    DuplicateItemError,
    ItemNotFoundError,
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


def _item_response(item, already_reviewed: bool) -> BucketItemResponse:
    return BucketItemResponse(
        id=str(item.id),
        album_id=str(item.album_id),
        position=item.position,
        note=item.note,
        status=item.status,
        post_id=str(item.post_id) if item.post_id else None,
        rec_reason=item.rec_reason,
        already_reviewed=already_reviewed,
        album=_album_brief(item.album),
    )


# ── reads (edge_guard only — no JWT; covered by GET /api/{proxy+}) ──────────────

@router.get("", response_model=BucketsResponse)
def list_buckets(
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
):
    buckets = svc.list_buckets(db)
    # Batch the already_reviewed lookup across every queued album (one query).
    all_album_ids = [str(it.album_id) for b in buckets for it in b.items]
    reviewed = svc.reviewed_album_ids(db, all_album_ids)
    return BucketsResponse(
        buckets=[
            BucketResponse(
                id=str(b.id),
                name=b.name,
                position=b.position,
                color=b.color,
                is_done=b.is_done,
                items=[
                    _item_response(it, str(it.album_id) in reviewed)
                    for it in b.items
                ],
            )
            for b in buckets
        ]
    )


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
        items=[],
    )


@router.patch("/{bucket_id}", response_model=BucketResponse)
def update_bucket(
    bucket_id: str,
    req: UpdateBucketRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
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
            status_code=409, detail='"작성 완료" 버킷은 하나만 지정할 수 있습니다.'
        )
    reviewed = svc.reviewed_album_ids(db, [str(it.album_id) for it in bucket.items])
    return BucketResponse(
        id=str(bucket.id),
        name=bucket.name,
        position=bucket.position,
        color=bucket.color,
        is_done=bucket.is_done,
        items=[
            _item_response(it, str(it.album_id) in reviewed) for it in bucket.items
        ],
    )


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
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        svc.reorder(db, [b.model_dump() for b in req.buckets])
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)


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
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        item = svc.add_item(db, bucket_id, album_id=req.album_id, note=req.note)
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except DuplicateItemError:
        raise HTTPException(status_code=409, detail="Album already in this bucket")
    reviewed = svc.reviewed_album_ids(db, [str(item.album_id)])
    return _item_response(item, str(item.album_id) in reviewed)


@router.patch(
    "/{bucket_id}/items/{item_id}", response_model=BucketItemResponse
)
def update_item(
    bucket_id: str,
    item_id: str,
    req: UpdateBucketItemRequest,
    db: Session = Depends(get_db),
    svc: BucketService = Depends(get_bucket_service),
    _claims: Dict = Depends(require_cognito_token),
):
    updates = req.model_dump(exclude_unset=True)
    try:
        item = svc.update_item(db, bucket_id, item_id, **updates)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    reviewed = svc.reviewed_album_ids(db, [str(item.album_id)])
    return _item_response(item, str(item.album_id) in reviewed)


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
