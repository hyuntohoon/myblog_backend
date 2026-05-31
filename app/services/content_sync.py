"""Keep the static content repo (MDX) in sync with post lifecycle changes.

FEAT-post-edit-delete-ui Step 3. The GitHub token is a backend-only secret, so
publish/un-publish must both live here (the frontend physically cannot touch the
content repo). Un-publishing (archive or hard delete) removes the MDX; restoring
re-publishes it from the DB row, keeping archive ↔ restore symmetric.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.publish_service import (
    content_path,
    github_delete_file,
    publish_to_github,
)
from myblog_shared_db.models import Album, Post

logger = logging.getLogger(__name__)


def _github_config() -> Optional[tuple[str, str, str]]:
    owner = settings.GITHUB_REPO_OWNER
    repo = settings.GITHUB_REPO_NAME
    token = settings.GITHUB_TOKEN
    if not all([owner, repo, token]):
        logger.error(
            "Missing GitHub config for content sync: owner=%s repo=%s token_set=%s",
            owner,
            repo,
            bool(token),
        )
        return None
    return owner, repo, token


def derive_subject_meta(db: Session, album_ids: list[str]) -> tuple[bool, Optional[dict]]:
    """Derive (best_new, music_review) from a single subject album.

    Shared by the publish route and restore re-publish so both emit
    byte-identical frontmatter. Only meaningful when exactly one album is
    linked — otherwise returns (False, None).
    """
    best_new = False
    music_review: Optional[dict] = None
    if len(album_ids) == 1:
        al = db.query(Album).filter(Album.id == album_ids[0]).first()
        if al is not None:
            best_new = bool(getattr(al, "best_new", False))
            primary_artist = al.artists[0] if al.artists else None
            artist_genres: list[str] = []
            if primary_artist is not None and getattr(primary_artist, "genres", None):
                raw = primary_artist.genres or []
                artist_genres = [str(g) for g in raw if g]
            music_review = {
                "subject": "album",
                "title": al.title,
                "artists": [a.name for a in al.artists],
                "releaseDate": al.release_date.isoformat() if al.release_date else None,
                "genres": artist_genres,
                "label": al.label,
                "cover": {"src": al.cover_url} if al.cover_url else None,
            }
            # Drop empty/None keys so zod's schema defaults apply (mirrors the
            # publish route's frontmatter-tidying behavior).
            music_review = {k: v for k, v in music_review.items() if v not in (None, [], "")}
    return best_new, music_review


def remove_post_content(posted_date, slug: str) -> Optional[dict]:
    """Remove a post's published MDX from the content repo.

    Idempotent (a never-published post is a no-op — see github_delete_file).
    Returns None when GitHub config is absent so the caller's DB op still
    succeeds. Raises RuntimeError on a real GitHub error.
    """
    cfg = _github_config()
    if cfg is None:
        return None
    owner, repo, token = cfg
    path = content_path(settings.CONTENT_DIR, posted_date, slug)
    return github_delete_file(
        owner=owner,
        repo=repo,
        branch=settings.GITHUB_REPO_BRANCH,
        path=path,
        token=token,
    )


def republish_post_content(
    db: Session, post: Post, recommended_track_ids: list[str]
) -> Optional[dict]:
    """Re-publish a post's MDX from its DB row (restore symmetry).

    Re-derives every publish input from the row so a restored post lands at the
    same path with equivalent frontmatter. Returns None when GitHub config is
    absent; raises RuntimeError on a real GitHub error.
    """
    cfg = _github_config()
    if cfg is None:
        return None
    owner, repo, token = cfg
    album_ids = [str(a.id) for a in post.albums]
    artist_ids = [str(a.id) for a in post.artists]
    best_new, music_review = derive_subject_meta(db, album_ids)
    return publish_to_github(
        owner=owner,
        repo=repo,
        branch=settings.GITHUB_REPO_BRANCH,
        content_dir=settings.CONTENT_DIR,
        token=token,
        title=post.title,
        slug=post.slug,
        description=post.description or "",
        posted_date=post.posted_date,
        category=post.category.name if post.category else None,
        album_ids=album_ids,
        artist_ids=artist_ids,
        post_id=str(post.id),
        album_cover_url=post.album_cover_url,
        rating=float(post.rating) if post.rating is not None else None,
        rating_scale=post.rating_scale,
        body_mdx=post.body_mdx,
        best_new=best_new,
        recommended_track_ids=recommended_track_ids,
        music_review=music_review,
    )
