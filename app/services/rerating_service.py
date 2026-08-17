# app/services/rerating_service.py
# FEAT-album-rerating — 재평가 ("I withdrew my 평가 for this album and will rate
# it again after listening").
#
# Its own service over its own table (`pending_reratings`, V54), for the reason
# the schema forces: starting a 재평가 CLEARS the star and the one-liner, and
# clearing them deletes the `album_reviews` row outright whenever
# `review_candidate` is false (ck_album_reviews_state_not_empty, V50). There is
# no row left to carry a flag, so this cannot be a fourth facet on the rating
# row the way `review_candidate` is. Same Option-B shape as PlannedRatingService.
#
# The state ends in TWO ways and only one of them lives here:
#   - cancel()  — the caller undoes the 재평가; the snapshot is restored.
#   - completion — a new star lands via RatingService.upsert, which deletes the
#     pending row in ITS transaction. Deliberately not duplicated here: both the
#     profile 재평가 중 section and the 마이버킷 다시 들어볼 앨범 tile are views
#     of this table, so that single delete clears both surfaces and there is no
#     per-surface cleanup call anyone can forget.
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from myblog_shared_db.models import Album, AlbumRating, PendingRerating

from app.services.rating_service import AlbumNotFoundError, RatingService
from app.services.user_service import UserService


class NoRatingToRerateError(Exception):
    """The caller has no 평가 to withdraw for this album. Route maps to 409.

    Distinct from AlbumNotFoundError (the album itself is missing, 404): here the
    album exists and the caller simply never rated it — 재평가 is defined as
    redoing a finished 평가, so there is nothing to redo. A state that holds only
    `review_candidate` counts as no rating for this purpose, matching every
    other public-facet check in RatingService.
    """


class ReratingService:
    def __init__(self, users: Optional[UserService] = None):
        # Same lazy-provisioning reasoning as RatingService/PlannedRatingService:
        # the users row must exist before the FK insert below, and a member's
        # first authed action in a session can be this one.
        self._users = users or UserService()

    def start(
        self,
        db: Session,
        member_id: uuid.UUID,
        claims: Optional[Dict[str, Any]],
        album_id: uuid.UUID,
    ) -> PendingRerating:
        """Withdraw the caller's 평가 for an album and open a 재평가.

        ONE transaction on purpose. The snapshot insert and the rating strip must
        commit together: a crash between two commits would leave the star gone
        with nothing left to restore it from, which is exactly the data loss
        `previous_rating NOT NULL` exists to make impossible.

        Idempotent — starting a 재평가 that is already open returns the existing
        row untouched, and never re-snapshots (by then the live rating is already
        gone, so a second snapshot would overwrite the real one with nothing).
        """
        self._users.get_or_create(db, member_id, claims)

        if db.get(Album, album_id) is None:
            raise AlbumNotFoundError(str(album_id))

        existing = self._pending(db, member_id, album_id)
        if existing is not None:
            return existing

        state = db.scalar(
            select(AlbumRating).where(
                AlbumRating.user_id == member_id,
                AlbumRating.album_id == album_id,
            )
        )
        if state is None or state.rating is None:
            raise NoRatingToRerateError(str(album_id))

        pending = PendingRerating(
            user_id=member_id,
            album_id=album_id,
            previous_rating=state.rating,
            previous_comment=state.comment,
        )
        db.add(pending)
        # Same rule the delete paths use — the row survives only if a private
        # facet remains. commit=False keeps it inside this transaction.
        RatingService.strip_rating(db, state, commit=False)

        try:
            db.commit()
        except IntegrityError:
            # uq_pending_reratings_user_album — a concurrent start won. Its
            # snapshot is the valid one (it read the rating before either strip),
            # so return the winner rather than retrying.
            db.rollback()
            winner = self._pending(db, member_id, album_id)
            if winner is None:
                raise
            return winner

        db.refresh(pending)
        return pending

    def cancel(self, db: Session, member_id: uuid.UUID, album_id: uuid.UUID) -> None:
        """Undo an open 재평가 — restore the withdrawn 평가, drop the pending row.

        Idempotent: cancelling a 재평가 that is not open is a no-op, not a 404,
        matching PlannedRatingService.unmark's delete-is-the-exit semantics.

        The restored rating carries TODAY's `created_at` when the original row
        had been deleted (the usual case — only a `review_candidate` mark keeps
        it alive). Accepted rather than snapshotting the original timestamp: the
        member is re-affirming the score today, and the profile feed's
        newest-first order reflecting that is defensible.
        """
        pending = self._pending(db, member_id, album_id)
        if pending is None:
            return

        state = db.scalar(
            select(AlbumRating).where(
                AlbumRating.user_id == member_id,
                AlbumRating.album_id == album_id,
            )
        )
        if state is None:
            state = AlbumRating(
                user_id=member_id,
                album_id=album_id,
                rating=pending.previous_rating,
                comment=pending.previous_comment,
            )
            db.add(state)
        else:
            # The row survived the withdrawal (review_candidate was set), or was
            # recreated by a mark while the 재평가 was open. Either way only the
            # public facet is restored — the private mark is not this call's to
            # touch.
            state.rating = pending.previous_rating
            state.comment = pending.previous_comment
            state.updated_at = func.now()

        db.delete(pending)
        db.commit()

    def list_pending(
        self, db: Session, user_id: uuid.UUID
    ) -> List[Tuple[PendingRerating, Album]]:
        """A member's open 재평가 rows, most recently started first.

        Serves BOTH the caller's private list and the public profile section, so
        it takes a plain user_id rather than reading the acting member: the
        difference between the two is which COLUMNS the route serializes
        (`previous_rating`/`previous_comment` are author-only), never which rows
        it may see.
        """
        rows = db.execute(
            select(PendingRerating, Album)
            .join(Album, PendingRerating.album_id == Album.id)
            .where(PendingRerating.user_id == user_id)
            .order_by(PendingRerating.created_at.desc())
        ).all()
        return [(r, a) for r, a in rows]

    @staticmethod
    def _pending(
        db: Session, user_id: uuid.UUID, album_id: uuid.UUID
    ) -> Optional[PendingRerating]:
        return db.scalar(
            select(PendingRerating).where(
                PendingRerating.user_id == user_id,
                PendingRerating.album_id == album_id,
            )
        )
