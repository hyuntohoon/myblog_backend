# app/api/routes/reviews.py
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    PostReviewBundle,
    PostReviewTrack,
    TrackReviewBatchRequest,
    TrackReviewUpsert,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_review_service
from app.services.review_service import (
    InvalidTrackError,
    PostNotFoundError,
    ReviewService,
)

router = APIRouter()


@router.get("/posts/{post_id}/reviews", response_model=PostReviewBundle)
def get_post_reviews(
    post_id: str,
    db: Session = Depends(get_db),
    svc: ReviewService = Depends(get_review_service),
):
    try:
        return svc.get_bundle(db, post_id)
    except PostNotFoundError:
        raise HTTPException(status_code=404, detail="Post not found")


@router.put(
    "/posts/{post_id}/reviews/tracks/{track_id}",
    response_model=PostReviewTrack,
)
def put_track_review(
    post_id: str,
    track_id: str,
    req: TrackReviewUpsert,
    db: Session = Depends(get_db),
    svc: ReviewService = Depends(get_review_service),
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        return svc.upsert_track_review(
            db,
            post_id=post_id,
            track_id=track_id,
            rating=req.rating,
            scale=req.scale,
            notes=req.notes,
        )
    except PostNotFoundError:
        raise HTTPException(status_code=404, detail="Post not found")
    except InvalidTrackError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=f"DB constraint violation: {e.orig}")


@router.delete("/posts/{post_id}/reviews/tracks/{track_id}", status_code=204)
def delete_track_review(
    post_id: str,
    track_id: str,
    db: Session = Depends(get_db),
    svc: ReviewService = Depends(get_review_service),
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        deleted = svc.delete_track_review(db, post_id, track_id)
    except PostNotFoundError:
        raise HTTPException(status_code=404, detail="Post not found")
    if not deleted:
        raise HTTPException(status_code=404, detail="Review not found")


@router.post(
    "/posts/{post_id}/reviews/tracks/batch",
    response_model=PostReviewBundle,
)
def batch_upsert_track_reviews(
    post_id: str,
    req: TrackReviewBatchRequest,
    db: Session = Depends(get_db),
    svc: ReviewService = Depends(get_review_service),
    _claims: Dict = Depends(require_cognito_token),
):
    items = [item.model_dump() for item in req.tracks]
    try:
        svc.batch_upsert_track_reviews(db, post_id=post_id, items=items)
    except PostNotFoundError:
        raise HTTPException(status_code=404, detail="Post not found")
    except InvalidTrackError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=f"DB constraint violation: {e.orig}")

    return svc.get_bundle(db, post_id)
