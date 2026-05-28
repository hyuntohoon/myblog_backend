from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_cognito_token
from app.core.config import settings
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


@router.post("")
def create_post(
    req: CreatePostReq,
    claims: Dict[str, Any] = Depends(require_cognito_token),
):
    owner = settings.GITHUB_REPO_OWNER
    repo = settings.GITHUB_REPO_NAME
    token = settings.GITHUB_TOKEN

    if not all([owner, repo, token]):
        logger.error("Missing GitHub config: owner=%s repo=%s token_set=%s", owner, repo, bool(token))
        raise HTTPException(500, detail="Missing GitHub environment variables")

    slug = req.slug or slugify(req.title)

    author = (
        claims.get("name")
        or claims.get("preferred_username")
        or claims.get("cognito:username")
        or claims.get("email")
    )

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
            author=author,
        )
    except RuntimeError as e:
        raise HTTPException(502, detail=str(e))

    return result
