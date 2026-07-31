# app/api/routes/reviews.py
# FEAT-multi-user-accounts Phase 1 — RYM-style public album reviews.
#   GET    /api/reviews/albums/{album_id}  — public aggregate (avg/count) + list
#                                            (rides the edge_guard GET catch-all).
#   PUT    /api/reviews/albums/{album_id}  — upsert MY review (member; JWT route).
#   DELETE /api/reviews/albums/{album_id}  — delete MY review (member; JWT route).
#   DELETE /api/reviews/{review_id}        — owner delete-any (require_owner; JWT).
import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.routes.me import _member_id
from app.api.schemas import (
    AlbumRatingAggregateResponse,
    AlbumRatingResponse,
    AlbumRatingUpsertRequest,
    RatingAuthor,
)
from app.core.auth import require_cognito_token, require_owner
from app.core.config import get_settings
from app.db.session import get_db
from app.di import get_rating_service
from app.services.rating_service import (
    AlbumNotFoundError,
    RatingNotFoundError,
    RatingRateLimitError,
    RatingService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _review_response(review, user) -> AlbumRatingResponse:
    return AlbumRatingResponse(
        id=str(review.id),
        album_id=str(review.album_id),
        author=RatingAuthor(
            handle=user.handle,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
        ),
        rating=float(review.rating),
        comment=review.comment,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _parse_album_id(album_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(album_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Album not found")


@router.get("/albums/{album_id}", response_model=AlbumRatingAggregateResponse)
def get_album_reviews(
    album_id: str,
    db: Session = Depends(get_db),
    svc: RatingService = Depends(get_rating_service),
):
    aid = _parse_album_id(album_id)
    average, count, rows = svc.album_aggregate(db, aid)
    return AlbumRatingAggregateResponse(
        album_id=album_id,
        average=average,
        count=count,
        reviews=[_review_response(r, u) for r, u in rows],
    )


@router.put("/albums/{album_id}", response_model=AlbumRatingResponse)
def put_album_review(
    album_id: str,
    payload: AlbumRatingUpsertRequest,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: RatingService = Depends(get_rating_service),
):
    aid = _parse_album_id(album_id)
    try:
        review, user = svc.upsert(
            db,
            _member_id(claims),
            claims,
            aid,
            payload.rating,
            payload.comment,
            daily_cap=get_settings().REVIEW_DAILY_CAP,
        )
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except RatingRateLimitError:
        raise HTTPException(
            status_code=429, detail="Daily review limit reached — try again later"
        )
    return _review_response(review, user)


@router.delete("/albums/{album_id}", status_code=204)
def delete_my_album_review(
    album_id: str,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: RatingService = Depends(get_rating_service),
):
    aid = _parse_album_id(album_id)
    try:
        svc.delete_own(db, _member_id(claims), aid)
    except RatingNotFoundError:
        raise HTTPException(status_code=404, detail="Review not found")
    return Response(status_code=204)


@router.delete("/{review_id}", status_code=204)
def owner_delete_review(
    review_id: str,
    claims: Dict[str, Any] = Depends(require_owner),
    db: Session = Depends(get_db),
    svc: RatingService = Depends(get_rating_service),
):
    try:
        rid = uuid.UUID(review_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Review not found")
    try:
        svc.delete_any(db, rid)
    except RatingNotFoundError:
        raise HTTPException(status_code=404, detail="Review not found")
    return Response(status_code=204)
