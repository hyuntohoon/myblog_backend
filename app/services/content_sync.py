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
from myblog_shared_db.models import Album, AlbumGenre, Genre, Post, post_genres_table

logger = logging.getLogger(__name__)


def derive_subgenres(db: Session, post_id: str) -> list[dict]:
    """Editorial sub-genre tags ({slug, label}) for a post's frontmatter.

    FEAT-genre-subgenres Step 3. The owner-tagged tier-1 sub-genres live in
    `post_genres` (keyed by post_id), distinct from `music_review.genres` (the
    machine tier-0 *album* labels). Derived from the DB — not the request — so
    the publish route and the restore re-publish emit identical frontmatter, the
    same way `derive_subject_meta` works. Ordered by the genre's display
    `position` (matches /genres ordering). Empty when the post has no genre tags
    or `post_id` is blank.
    """
    if not post_id:
        return []
    rows = (
        db.query(Genre.slug, Genre.label)
        .join(post_genres_table, post_genres_table.c.genre_id == Genre.id)
        .filter(post_genres_table.c.post_id == post_id)
        .order_by(Genre.position)
        .all()
    )
    return [{"slug": slug, "label": label} for slug, label in rows]


def _album_genre_labels(db: Session, album_id: str) -> list[str]:
    """Real tier-0 genre labels for an album's frontmatter (FEAT-genre-system F8).

    Prefers high-confidence rows; falls back to the album's own low-confidence
    rows only when no high-confidence row exists. (Owner decision 2026-06-13:
    high-first, else low. The RFC's "zero-high should not happen post-Step 3"
    was wrong — 269/983 albums are low-only, all carrying real 12-vocab English
    labels, so emitting their low rows beats reverting to the artist-copy fake.)
    Ordered by the genre's display `position` (matches /genres ordering).
    Returns [] only when the album has no album_genres rows at all.
    """
    rows = (
        db.query(Genre.label, AlbumGenre.confidence)
        .join(AlbumGenre, AlbumGenre.genre_id == Genre.id)
        .filter(AlbumGenre.album_id == album_id)
        .order_by(Genre.position)
        .all()
    )
    high = [label for label, conf in rows if conf == "high"]
    return high or [label for label, _conf in rows]


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
            # Real tier-0 genres from album_genres; artist-copy strings are only a
            # fallback for an album with no album_genres rows (FEAT-genre-system F8).
            genres = _album_genre_labels(db, album_ids[0])
            if not genres:
                primary_artist = al.artists[0] if al.artists else None
                if primary_artist is not None and getattr(primary_artist, "genres", None):
                    genres = [str(g) for g in (primary_artist.genres or []) if g]
            music_review = {
                "subject": "album",
                "title": al.title,
                "artists": [a.name for a in al.artists],
                "releaseDate": al.release_date.isoformat() if al.release_date else None,
                "genres": genres,
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
    subgenres = derive_subgenres(db, str(post.id))
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
        category=post.section.name if post.section else None,
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
        tags=[t.name for t in post.tags],
        subgenres=subgenres,
    )
