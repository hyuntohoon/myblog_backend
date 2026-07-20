# app/api/routes/members.py
# FEAT-multi-user-accounts Phase 1 — public member profiles (RYM-style).
#   GET /api/members             — index of members with ≥1 review (front
#                                  getStaticPaths for static profile prerender).
#   GET /api/members/{handle}    — a member's public profile + review feed.
#   GET /api/members/{handle}/now-playing — a member's public now-playing, merged
#                                  across the Last.fm + Spotify member caches
#                                  (DB only, worker-written — rule #9), with
#                                  source provenance (OQ7).
# All are public reads and ride the edge_guard GET catch-all (no JWT route).
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import (
    MemberListResponse,
    MemberNowPlayingResponse,
    MemberProfileResponse,
    MemberReviewResponse,
    MemberSummary,
)
from app.db.session import get_db
from app.di import get_integration_service, get_review_service
from app.services.artist_primary import primary_artist_map
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
    # ui-unify Step 4 (평가 artist line): one batch resolve for the whole feed —
    # the feed albums' primary artists, keyed by album id.
    amap = primary_artist_map(db, [a.id for _, a in rows])
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
                artist_id=(amap.get(str(a.id)) or (None, None))[0],
                artist_name=(amap.get(str(a.id)) or (None, None))[1],
                rating=float(r.rating),
                comment=r.comment,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r, a in rows
        ],
    )


@router.get("/{handle}/now-playing", response_model=MemberNowPlayingResponse)
def member_now_playing(
    handle: str,
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    """The member's public now-playing, source-merged across the worker-written
    lastfm_recent_tracks + spotify_member_now_playing caches (rule #9 — never a
    synchronous provider call). Carries provenance: `source`, and for Last.fm
    picks `source_username` ("via Last.fm @x" — unverified-username transparency,
    OQ7). 404 for an unknown handle; is_playing=false covers both 'not connected'
    and 'nothing playing' so a member's integration status stays private —
    the profile page hides the section unless is_playing is true."""
    try:
        pick = svc.public_now_playing(db, handle)
    except MemberNotFoundError:
        raise HTTPException(status_code=404, detail="Member not found")
    if pick is None:
        return MemberNowPlayingResponse(is_playing=False)
    return MemberNowPlayingResponse(
        is_playing=True,
        artist=pick.artist,
        track=pick.track,
        album=pick.album,
        image_url=pick.image_url,
        played_at=pick.played_at,
        source=pick.source,  # type: ignore[arg-type]
        source_username=pick.source_username,
    )
