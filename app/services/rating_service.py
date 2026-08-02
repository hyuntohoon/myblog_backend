# app/services/rating_service.py
# FEAT-multi-user-accounts Phase 1 — RYM-style public album ratings over the V38
# `album_reviews` table. One rating (0.5–5.0 half-steps) + optional comment per
# member per album; aggregates (avg/count) computed LIVE at read time (OQ2 — no
# denormalized counter). See docs/rfcs/FEAT-multi-user-accounts.md.
#
# FEAT-album-review-authoring Step 1 (V50): a row is now "the member's STATE for
# this album" and the rating is only its PUBLIC FACET. The private facet is
# `review_candidate` ("I'll write a 평론 about this later"). Two rules hold this
# together, and both live here rather than in the routes:
#
#   1. EVERY PUBLIC READ FILTERS `rating IS NOT NULL`. A rating-less state is
#      private data sitting in a public table; a missing filter publishes it.
#      test_rating_service_db.py has a regression test per public read.
#   2. A state with no facet left does not exist. Whatever empties the last one
#      deletes the row, so "no rating" and "no mark" can't accumulate as ghosts.
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from myblog_shared_db.models import Album, AlbumRating, User

from app.services.user_service import UserService

logger = logging.getLogger(__name__)


class AlbumNotFoundError(Exception):
    """The target album does not exist. Route maps to 404."""


class RatingNotFoundError(Exception):
    """No matching review to delete. Route maps to 404."""


class MemberNotFoundError(Exception):
    """No member with that handle. Route maps to 404."""


class RatingRateLimitError(Exception):
    """Per-member daily create cap hit. Route maps to 429."""


class RatingService:
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
        changes: Mapping[str, Any],
        *,
        daily_cap: int,
    ) -> Tuple[Optional[AlbumRating], User]:
        """Apply a PARTIAL change to the member's state for an album.

        `changes` carries only the facets the caller actually sent (`rating`,
        `comment`, `review_candidate`); anything absent keeps its stored value.
        That is what lets the album surface save a star and the bucket surface
        flip the mark without either overwriting the other from a stale read.

        The (user_id, album_id) UNIQUE (V38) makes this an upsert: a second call
        edits the existing row. The daily cap counts CREATES only — editing an
        existing state is always allowed (no way to abuse volume by re-rating one
        album). Album existence is checked explicitly so a bad album_id is a clean
        404 rather than an FK-violation 500.

        Returns (None, user) when the change leaves no facet behind: the row is
        deleted, or never created. An empty state does not exist (V50 CHECK), and
        the route turns that None into a 204.
        """
        user = self._users.get_or_create(db, member_id, claims)

        if db.get(Album, album_id) is None:
            raise AlbumNotFoundError(str(album_id))

        state = db.scalar(
            select(AlbumRating).where(
                AlbumRating.user_id == user.id,
                AlbumRating.album_id == album_id,
            )
        )

        if state is None:
            rating = changes.get("rating")
            # A one-liner belongs to a rating (ck_album_reviews_comment_needs_rating).
            comment = changes.get("comment") if rating is not None else None
            candidate = bool(changes.get("review_candidate", False))
            if rating is None and not candidate:
                # Nothing to store. Not an error — "clear what isn't there" is a
                # no-op, and it must not burn a create against the daily cap.
                return None, user

            recent = db.scalar(
                select(func.count())
                .select_from(AlbumRating)
                .where(
                    AlbumRating.user_id == user.id,
                    AlbumRating.created_at > func.now() - text("interval '24 hours'"),
                )
            )
            if recent is not None and recent >= daily_cap:
                raise RatingRateLimitError(f"{recent}/{daily_cap} in 24h")
            state = AlbumRating(
                user_id=user.id,
                album_id=album_id,
                rating=rating,
                comment=comment,
                review_candidate=candidate,
            )
            db.add(state)
        else:
            if "rating" in changes:
                state.rating = changes["rating"]
            if "comment" in changes:
                state.comment = changes["comment"]
            if "review_candidate" in changes:
                state.review_candidate = bool(changes["review_candidate"])
            # Dropping the rating drops its one-liner with it — the comment is
            # part of the 평가, and the CHECK would reject the pair anyway.
            if state.rating is None:
                state.comment = None
            if state.rating is None and not state.review_candidate:
                db.delete(state)
                db.commit()
                return None, user
            state.updated_at = func.now()

        db.commit()
        db.refresh(state)
        return state, user

    def delete_own(self, db: Session, member_id: uuid.UUID, album_id: uuid.UUID) -> None:
        """Delete the member's own 평가 (star + one-liner) for an album.

        The private mark survives: deleting a public rating is not a request to
        forget that the album is an editorial candidate. The row goes only when
        the rating was its last facet. 404 when there is no rating to delete —
        a mark-only state included, since it holds nothing public.
        """
        state = db.scalar(
            select(AlbumRating).where(
                AlbumRating.user_id == member_id,
                AlbumRating.album_id == album_id,
            )
        )
        if state is None or state.rating is None:
            raise RatingNotFoundError(str(album_id))
        self._strip_rating(db, state)

    def delete_any(self, db: Session, review_id: uuid.UUID) -> None:
        """Owner moderation: delete any 평가 by id (require_owner). 404 if absent.

        Same rule as delete_own — moderating someone's public rating must not
        erase their private mark along with it.
        """
        state = db.get(AlbumRating, review_id)
        if state is None or state.rating is None:
            raise RatingNotFoundError(str(review_id))
        self._strip_rating(db, state)
        logger.info("owner deleted rating %s", review_id)

    @staticmethod
    def _strip_rating(db: Session, state: AlbumRating) -> None:
        """Drop the public facet, keeping the row only if a private one remains."""
        if state.review_candidate:
            state.rating = None
            state.comment = None
            state.updated_at = func.now()
        else:
            db.delete(state)
        db.commit()

    # ── reads (public) ───────────────────────────────────────────────────────

    # Every query below is PUBLIC and therefore carries `rating IS NOT NULL`
    # (V50). Without it a state that exists only to hold a private editorial mark
    # would be published as a 평가 — with a null star, on someone else's screen.
    _PUBLIC = AlbumRating.rating.is_not(None)

    def album_aggregate(
        self, db: Session, album_id: uuid.UUID
    ) -> Tuple[Optional[float], int, List[Tuple[AlbumRating, User]]]:
        """Live (avg, count, [(rating, author)…]) for an album, newest-first.

        No album-existence check: an album with zero ratings returns (None, 0, [])
        — a valid public answer, and the album may legitimately have none.
        """
        avg, count = db.execute(
            select(func.avg(AlbumRating.rating), func.count(AlbumRating.id)).where(
                AlbumRating.album_id == album_id, self._PUBLIC
            )
        ).one()
        rows = db.execute(
            select(AlbumRating, User)
            .join(User, AlbumRating.user_id == User.id)
            .where(AlbumRating.album_id == album_id, self._PUBLIC)
            .order_by(AlbumRating.created_at.desc())
        ).all()
        average = round(float(avg), 2) if avg is not None else None
        return average, int(count or 0), [(r, u) for r, u in rows]

    def member_profile(
        self, db: Session, handle: str
    ) -> Tuple[User, List[Tuple[AlbumRating, Album]]]:
        """A member's public identity + newest-first rating feed (each joined to
        its album for render). 404 if the handle is unknown."""
        user = db.scalar(select(User).where(User.handle == handle.lower()))
        if user is None:
            raise MemberNotFoundError(handle)
        rows = db.execute(
            select(AlbumRating, Album)
            .join(Album, AlbumRating.album_id == Album.id)
            .where(AlbumRating.user_id == user.id, self._PUBLIC)
            .order_by(AlbumRating.created_at.desc())
        ).all()
        return user, [(r, a) for r, a in rows]

    def list_members(
        self, db: Session, limit: int = 1000
    ) -> List[Tuple[User, int]]:
        """Members with ≥1 rating + their rating count — the front's static
        profile-prerender index (getStaticPaths). Ordered by volume so the most
        active profiles are prerendered first if the limit ever bites.

        The public filter lives in the JOIN condition, not the WHERE: an inner
        join plus a WHERE would be equivalent here, but keeping it on the join
        makes it impossible to later widen this to an outer join and silently
        start counting mark-only states.
        """
        rows = db.execute(
            select(User, func.count(AlbumRating.id).label("n"))
            .join(AlbumRating, (AlbumRating.user_id == User.id) & self._PUBLIC)
            .group_by(User.id)
            .order_by(func.count(AlbumRating.id).desc())
            .limit(limit)
        ).all()
        return [(u, int(n)) for u, n in rows]

    # ── reads (author-private) ───────────────────────────────────────────────

    def my_states(
        self,
        db: Session,
        member_id: uuid.UUID,
        album_id: Optional[uuid.UUID] = None,
    ) -> List[AlbumRating]:
        """The caller's OWN states, private facet included.

        Scoped to `member_id` by construction — the only guard that matters here,
        since these rows carry the editorial marks. No lazy provisioning: a member
        who has never written anything simply has no states, and a read should not
        create a users row.
        """
        stmt = select(AlbumRating).where(AlbumRating.user_id == member_id)
        if album_id is not None:
            stmt = stmt.where(AlbumRating.album_id == album_id)
        return list(db.scalars(stmt.order_by(AlbumRating.updated_at.desc())))

    def my_review_candidates(
        self, db: Session, member_id: uuid.UUID
    ) -> List[Tuple[AlbumRating, Album]]:
        """The caller's "평론 쓸 것" queue (Step 2): marked states joined to their
        albums, most recently touched first.

        Deliberately NOT a client-side filter over my_states. Two reasons:

          1. `review_candidate IS TRUE` is the whole definition of this list. In
             the query it cannot be forgotten; in the caller it can.
          2. A mark can sit on an album that is in no bucket and carries no rating,
             so the row has no other source of a title — hence the album join,
             which my_states does not need (its callers key by album_id alone).

        Private, like every mark read: scoped to `member_id` by construction, with
        no parameter that could widen it to someone else. The join is an inner one
        — album_id is a FK, so a state without an album cannot exist.
        """
        rows = db.execute(
            select(AlbumRating, Album)
            .join(Album, AlbumRating.album_id == Album.id)
            .where(
                AlbumRating.user_id == member_id,
                AlbumRating.review_candidate.is_(True),
            )
            .order_by(AlbumRating.updated_at.desc())
        ).all()
        return [(r, a) for r, a in rows]
