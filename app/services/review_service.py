# app/services/review_service.py
# FEAT-multi-user-accounts Phase 1 — RYM-style public album reviews over the V38
# `album_reviews` table. One rating (0.5–5.0 half-steps) + optional comment per
# member per album; aggregates (avg/count) computed LIVE at read time (OQ2 — no
# denormalized counter). All reads public. See docs/rfcs/FEAT-multi-user-accounts.md.
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from myblog_shared_db.models import Album, AlbumReview, User

from app.services.user_service import UserService

logger = logging.getLogger(__name__)


class AlbumNotFoundError(Exception):
    """The target album does not exist. Route maps to 404."""


class ReviewNotFoundError(Exception):
    """No matching review to delete. Route maps to 404."""


class MemberNotFoundError(Exception):
    """No member with that handle. Route maps to 404."""


class ReviewRateLimitError(Exception):
    """Per-member daily create cap hit. Route maps to 429."""


class ReviewService:
    def __init__(self, users: Optional[UserService] = None):
        # Reuse the lazy-provisioning path so a member's first authed action can be
        # a review (they may never have hit /api/me).
        self._users = users or UserService()

    # ── writes (member) ──────────────────────────────────────────────────────

    def upsert(
        self,
        db: Session,
        member_id: uuid.UUID,
        claims: Optional[Dict[str, Any]],
        album_id: uuid.UUID,
        rating: float,
        comment: Optional[str],
        *,
        daily_cap: int,
    ) -> Tuple[AlbumReview, User]:
        """Create or replace the member's single review for an album.

        The (user_id, album_id) UNIQUE (V38) makes this an upsert: a second call
        edits the existing row. The daily cap counts CREATES only — editing an
        existing review is always allowed (no way to abuse volume by re-rating one
        album). Album existence is checked explicitly so a bad album_id is a clean
        404 rather than an FK-violation 500.
        """
        user = self._users.get_or_create(db, member_id, claims)

        if db.get(Album, album_id) is None:
            raise AlbumNotFoundError(str(album_id))

        review = db.scalar(
            select(AlbumReview).where(
                AlbumReview.user_id == user.id,
                AlbumReview.album_id == album_id,
            )
        )

        if review is None:
            recent = db.scalar(
                select(func.count())
                .select_from(AlbumReview)
                .where(
                    AlbumReview.user_id == user.id,
                    AlbumReview.created_at > func.now() - text("interval '24 hours'"),
                )
            )
            if recent is not None and recent >= daily_cap:
                raise ReviewRateLimitError(f"{recent}/{daily_cap} in 24h")
            review = AlbumReview(
                user_id=user.id, album_id=album_id, rating=rating, comment=comment
            )
            db.add(review)
        else:
            review.rating = rating
            review.comment = comment
            review.updated_at = func.now()

        db.commit()
        db.refresh(review)
        return review, user

    def delete_own(self, db: Session, member_id: uuid.UUID, album_id: uuid.UUID) -> None:
        """Delete the member's own review for an album. 404 if absent."""
        review = db.scalar(
            select(AlbumReview).where(
                AlbumReview.user_id == member_id,
                AlbumReview.album_id == album_id,
            )
        )
        if review is None:
            raise ReviewNotFoundError(str(album_id))
        db.delete(review)
        db.commit()

    def delete_any(self, db: Session, review_id: uuid.UUID) -> None:
        """Owner moderation: delete any review by id (require_owner). 404 if absent."""
        review = db.get(AlbumReview, review_id)
        if review is None:
            raise ReviewNotFoundError(str(review_id))
        db.delete(review)
        db.commit()
        logger.info("owner deleted review %s", review_id)

    # ── reads (public) ───────────────────────────────────────────────────────

    def album_aggregate(
        self, db: Session, album_id: uuid.UUID
    ) -> Tuple[Optional[float], int, List[Tuple[AlbumReview, User]]]:
        """Live (avg, count, [(review, author)…]) for an album, newest-first.

        No album-existence check: an album with zero reviews returns (None, 0, [])
        — a valid public answer, and the album may legitimately have no reviews.
        """
        avg, count = db.execute(
            select(func.avg(AlbumReview.rating), func.count(AlbumReview.id)).where(
                AlbumReview.album_id == album_id
            )
        ).one()
        rows = db.execute(
            select(AlbumReview, User)
            .join(User, AlbumReview.user_id == User.id)
            .where(AlbumReview.album_id == album_id)
            .order_by(AlbumReview.created_at.desc())
        ).all()
        average = round(float(avg), 2) if avg is not None else None
        return average, int(count or 0), [(r, u) for r, u in rows]

    def member_profile(
        self, db: Session, handle: str
    ) -> Tuple[User, List[Tuple[AlbumReview, Album]]]:
        """A member's public identity + newest-first review feed (each joined to
        its album for render). 404 if the handle is unknown."""
        user = db.scalar(select(User).where(User.handle == handle.lower()))
        if user is None:
            raise MemberNotFoundError(handle)
        rows = db.execute(
            select(AlbumReview, Album)
            .join(Album, AlbumReview.album_id == Album.id)
            .where(AlbumReview.user_id == user.id)
            .order_by(AlbumReview.created_at.desc())
        ).all()
        return user, [(r, a) for r, a in rows]

    def list_members(
        self, db: Session, limit: int = 1000
    ) -> List[Tuple[User, int]]:
        """Members with ≥1 review + their review count — the front's static
        profile-prerender index (getStaticPaths). Ordered by volume so the most
        active profiles are prerendered first if the limit ever bites."""
        rows = db.execute(
            select(User, func.count(AlbumReview.id).label("n"))
            .join(AlbumReview, AlbumReview.user_id == User.id)
            .group_by(User.id)
            .order_by(func.count(AlbumReview.id).desc())
            .limit(limit)
        ).all()
        return [(u, int(n)) for u, n in rows]
