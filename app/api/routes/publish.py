from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_cognito_token
from app.core.config import settings
from app.db.session import get_db
from app.services.content_sync import derive_subject_meta
from app.services.publish_service import publish_to_github, slugify

logger = logging.getLogger(__name__)

router = APIRouter()


class CreatePostReq(BaseModel):
    title: str = Field(min_length=1)
    body_mdx: str | None = None
    category: str | None = None
    description: str = ""
    posted_date: date
    slug: str | None = None
    post_id: str | None = None
    album_ids: list[str] = Field(default_factory=list)
    artist_ids: list[str] = Field(default_factory=list)
    album_cover_url: str | None = None
    rating: float | None = None
    # FEAT-view-redesign Step 5: writer's ★ picks (track IDs). Written to
    # the MDX frontmatter so the static read page can render the album
    # tracklist with picks marked without a runtime auth-gated lookup.
    recommended_track_ids: list[str] = Field(default_factory=list)


@router.post("")
def create_post(
    req: CreatePostReq,
    db: Session = Depends(get_db),
    _claims: Dict[str, Any] = Depends(require_cognito_token),
):
    owner = settings.GITHUB_REPO_OWNER
    repo = settings.GITHUB_REPO_NAME
    token = settings.GITHUB_TOKEN

    if not all([owner, repo, token]):
        logger.error("Missing GitHub config: owner=%s repo=%s token_set=%s", owner, repo, bool(token))
        raise HTTPException(500, detail="Missing GitHub environment variables")

    slug = req.slug or slugify(req.title)

    # Read the subject album (when present) and derive both the
    # FEAT-writer-lowfreq-redesign Step 5 `best_new` flag and the
    # FEAT-view-redesign Step 5 follow-up `music_review` payload — the
    # read-page hero (.lfq-hero) reads label / year / artist / album-title /
    # genres from this MDX block so the static page doesn't need a runtime
    # album-detail fetch just to render the header. Shared with the restore
    # re-publish path (content_sync) so both emit identical frontmatter.
    best_new, music_review = derive_subject_meta(db, req.album_ids)

    try:
        result = publish_to_github(
            owner=owner,
            repo=repo,
            branch=settings.GITHUB_REPO_BRANCH,
            content_dir=settings.CONTENT_DIR,
            token=token,
            title=req.title,
            slug=slug,
            description=req.description,
            posted_date=req.posted_date,
            category=req.category,
            album_ids=req.album_ids,
            artist_ids=req.artist_ids,
            post_id=req.post_id or "",
            album_cover_url=req.album_cover_url,
            rating=req.rating,
            rating_scale=5,
            body_mdx=req.body_mdx,
            best_new=best_new,
            recommended_track_ids=req.recommended_track_ids,
            music_review=music_review,
        )
    except RuntimeError as e:
        raise HTTPException(502, detail=str(e))

    return result
