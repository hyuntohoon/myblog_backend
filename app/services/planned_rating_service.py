# app/services/planned_rating_service.py
# FEAT-rating-smart-collections Step 2 — 평가 예정 ("plan to rate"), Option B.
#
# Deliberately its own service over its own table (`planned_ratings`, V52), not
# folded into RatingService/AlbumRating. A row's mere existence is the mark;
# DELETE is the unmark — there is no partial-update shape to reuse from the
# rating upsert, and no CHECK-constraint coupling to worry about. Strictly
# separate from `review_candidate` (editorial intent) and from bucket
# membership (the front end renders this as a bucket-shaped tile, but no
# `review_buckets`/`review_bucket_items` row is ever touched here).
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from myblog_shared_db.models import Album, PlannedRating

from app.services.rating_service import AlbumNotFoundError
from app.services.user_service import UserService


class PlannedRatingService:
    def __init__(self, users: Optional[UserService] = None):
        # Same lazy-provisioning reasoning as RatingService: a member's first
        # authed action can be planning a rating, before they've ever hit
        # /api/me — the users row must exist before the FK insert below.
        self._users = users or UserService()

    def mark(
        self,
        db: Session,
        member_id: uuid.UUID,
        claims: Optional[Dict[str, Any]],
        album_id: uuid.UUID,
    ) -> PlannedRating:
        """Mark an album as 평가 예정. Idempotent — marking an already-planned
        album is a no-op, not an error (ON CONFLICT DO NOTHING on the same
        UNIQUE(user_id, album_id) V52 defines).

        Album existence is checked explicitly, matching RatingService.upsert's
        own reasoning: a bad album_id should be a clean 404, not an
        FK-violation 500.
        """
        self._users.get_or_create(db, member_id, claims)

        if db.get(Album, album_id) is None:
            raise AlbumNotFoundError(str(album_id))

        db.execute(
            pg_insert(PlannedRating)
            .values(user_id=member_id, album_id=album_id)
            .on_conflict_do_nothing(constraint="uq_planned_ratings_user_album")
        )
        db.commit()
        return db.scalar(
            select(PlannedRating).where(
                PlannedRating.user_id == member_id,
                PlannedRating.album_id == album_id,
            )
        )

    def unmark(self, db: Session, member_id: uuid.UUID, album_id: uuid.UUID) -> None:
        """Unmark. Idempotent — deleting a mark that does not exist is a no-op,
        matching PlannedRating's own delete-is-the-unmark semantics (no 404,
        unlike RatingService.delete_own which deletes a rating with real
        content the caller must already know exists)."""
        state = db.scalar(
            select(PlannedRating).where(
                PlannedRating.user_id == member_id,
                PlannedRating.album_id == album_id,
            )
        )
        if state is not None:
            db.delete(state)
            db.commit()

    def list_planned(
        self, db: Session, member_id: uuid.UUID
    ) -> List[Tuple[PlannedRating, Album]]:
        """The caller's 평가 예정 queue, most recently planned first. Private —
        scoped to `member_id` by construction, no parameter that could widen it
        to someone else's list."""
        rows = db.execute(
            select(PlannedRating, Album)
            .join(Album, PlannedRating.album_id == Album.id)
            .where(PlannedRating.user_id == member_id)
            .order_by(PlannedRating.created_at.desc())
        ).all()
        return [(r, a) for r, a in rows]
