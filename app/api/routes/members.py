# app/api/routes/members.py
# FEAT-multi-user-accounts Phase 1 — public member profiles (RYM-style).
#   GET /api/members             — index of members with ≥1 review (front
#                                  getStaticPaths for static profile prerender).
#   GET /api/members/{handle}    — a member's public profile + review feed.
#   GET /api/members/{handle}/now-playing — a member's public Last.fm now-playing
#                                  (DB cache only, worker-written — rule #9).
# All are public reads and ride the edge_guard GET catch-all (no JWT route).
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import (
    LastfmNowPlayingResponse,
    MemberListResponse,
    MemberProfileResponse,
    MemberReviewResponse,
    MemberSummary,
)
from app.db.session import get_db
from app.di import get_integration_service, get_review_service
from app.services.integration_service import IntegrationService
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


@router.get("/{handle}/now-playing", response_model=LastfmNowPlayingResponse)
def member_now_playing(
    handle: str,
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    """The member's public now-playing. Reads only the worker-written
    lastfm_recent_tracks cache (rule #9 — never a synchronous Last.fm call).
    404 for an unknown handle; is_playing=false covers both 'not connected'
    and 'nothing playing' so a member's integration status stays private —
    the profile page hides the section unless is_playing is true."""
    try:
        row = svc.public_now_playing(db, handle)
    except MemberNotFoundError:
        raise HTTPException(status_code=404, detail="Member not found")
    if row is None:
        return LastfmNowPlayingResponse(is_playing=False)
    return LastfmNowPlayingResponse(
        is_playing=True,
        artist=row.artist_name,
        track=row.track_name,
        album=row.album_name,
        image_url=row.image_url,
        played_at=row.played_at,
    )
