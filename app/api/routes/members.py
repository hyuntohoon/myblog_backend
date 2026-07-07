# app/api/routes/members.py
# FEAT-multi-user-accounts Phase 1 — public member profiles (RYM-style).
#   GET /api/members             — index of members with ≥1 review (front
#                                  getStaticPaths for static profile prerender).
#   GET /api/members/{handle}    — a member's public profile + review feed.
# Both are public reads and ride the edge_guard GET catch-all (no JWT route).
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import (
    MemberListResponse,
    MemberProfileResponse,
    MemberReviewResponse,
    MemberSummary,
)
from app.db.session import get_db
from app.di import get_review_service
from app.services.review_service import MemberNotFoundError, ReviewService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=MemberListResponse)
def list_members(
    db: Session = Depends(get_db),
    svc: ReviewService = Depends(get_review_service),
):
    rows = svc.list_members(db)
    return MemberListResponse(
        members=[
            MemberSummary(
                handle=u.handle,
                display_name=u.display_name,
                avatar_url=u.avatar_url,
                review_count=n,
            )
            for u, n in rows
        ]
    )


@router.get("/{handle}", response_model=MemberProfileResponse)
def get_member(
    handle: str,
    db: Session = Depends(get_db),
    svc: ReviewService = Depends(get_review_service),
):
    try:
        user, rows = svc.member_profile(db, handle)
    except MemberNotFoundError:
        raise HTTPException(status_code=404, detail="Member not found")
    return MemberProfileResponse(
        handle=user.handle,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
        review_count=len(rows),
        reviews=[
            MemberReviewResponse(
                id=str(r.id),
                album_id=str(r.album_id),
                album_title=a.title,
                album_cover_url=a.cover_url,
                rating=float(r.rating),
                comment=r.comment,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r, a in rows
        ],
    )
