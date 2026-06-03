# app/services/bucket_service.py
from __future__ import annotations

from datetime import date
from typing import List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from myblog_shared_db.models import (
    Album,
    ReviewBucket,
    ReviewBucketItem,
    post_albums_table as post_albums,
)


class BucketNotFoundError(Exception):
    """Raised when a bucket id does not exist. Route maps to 404."""


class ItemNotFoundError(Exception):
    """Raised when a bucket item id does not exist. Route maps to 404."""


class AlbumNotFoundError(Exception):
    """Raised when an album id does not exist. Route maps to 404."""


class DuplicateItemError(Exception):
    """Raised when an album is already in the target bucket. Route maps to 409.

    Distinct from `already_reviewed` (which is a non-blocking advisory badge for
    albums that have a published review): this guards the UNIQUE(bucket_id,
    album_id) constraint so the same album can't sit twice in one column.
    """


# Auto-recommendation: initial position is seeded from a weighted blend of two
# DB-only signals (RFC §"백엔드 API" — release recency + Spotify popularity).
# After the first placement, manual drag (PUT /reorder) is the source of truth.
W_RECENCY = 0.6
W_POPULARITY = 0.4
# Linear recency decay window: an album older than this scores 0 on recency.
RECENCY_WINDOW_DAYS = 365 * 2


class BucketService:
    """Review-queue kanban: user-created buckets holding queued albums.

    Single user → no user_id / ownership checks. Transaction boundary lives in
    the service (commit once per mutation), mirroring PostService.
    """

    # ── recommendation scoring ────────────────────────────────────────────────

    @staticmethod
    def _recency_score(release_date: Optional[date], *, today: date) -> float:
        if release_date is None:
            return 0.0
        age_days = (today - release_date).days
        if age_days <= 0:
            return 1.0
        return max(0.0, 1.0 - age_days / RECENCY_WINDOW_DAYS)

    @staticmethod
    def _popularity_score(popularity: Optional[int]) -> float:
        if popularity is None:
            return 0.0
        return min(1.0, max(0.0, popularity / 100.0))

    @classmethod
    def _score(cls, album: Album, *, today: date) -> tuple[float, Optional[str]]:
        rec = cls._recency_score(album.release_date, today=today)
        pop = cls._popularity_score(album.popularity)
        score = W_RECENCY * rec + W_POPULARITY * pop
        # rec_reason snapshots the dominant signal for the frontend chip.
        if rec <= 0 and pop <= 0:
            reason = None
        elif rec >= pop:
            reason = "신보"
        else:
            reason = "인기"
        return score, reason

    # ── reads ─────────────────────────────────────────────────────────────────

    def list_buckets(self, db: Session) -> List[ReviewBucket]:
        """All buckets (column order), each with items already position-ordered
        by the relationship's order_by."""
        return (
            db.query(ReviewBucket)
            .order_by(ReviewBucket.position, ReviewBucket.created_at)
            .all()
        )

    def reviewed_album_ids(self, db: Session, album_ids: Sequence) -> set:
        """Subset of the given album ids that already have a published review
        (appear in post_albums). One query, used for the already_reviewed badge."""
        if not album_ids:
            return set()
        rows = db.execute(
            select(post_albums.c.album_id).where(
                post_albums.c.album_id.in_(list(album_ids))
            )
        ).all()
        return {str(r.album_id) for r in rows}

    # ── bucket CRUD ─────────────────────────────────────────────────────────────

    def create_bucket(
        self, db: Session, *, name: str, color: Optional[str] = None
    ) -> ReviewBucket:
        name = (name or "").strip()
        if not name:
            raise ValueError("name required")
        next_pos = db.execute(
            select(func.coalesce(func.max(ReviewBucket.position), -1))
        ).scalar_one()
        bucket = ReviewBucket(name=name, color=color, position=int(next_pos) + 1)
        db.add(bucket)
        db.commit()
        db.refresh(bucket)
        return bucket

    def get_bucket(self, db: Session, bucket_id: str) -> Optional[ReviewBucket]:
        return db.query(ReviewBucket).filter(ReviewBucket.id == bucket_id).first()

    def update_bucket(
        self,
        db: Session,
        bucket_id: str,
        *,
        name: Optional[str] = None,
        color: Optional[str] = None,
        position: Optional[int] = None,
        is_done: Optional[bool] = None,
    ) -> ReviewBucket:
        bucket = self.get_bucket(db, bucket_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("name cannot be empty")
            bucket.name = name
        if color is not None:
            # empty string clears the color label
            bucket.color = color or None
        if position is not None:
            bucket.position = int(position)
        if is_done is not None:
            # The partial unique index (idx_review_buckets_single_done) enforces
            # at-most-one done bucket; a second true surfaces as IntegrityError →
            # the route maps it to 409.
            bucket.is_done = bool(is_done)
        db.commit()
        db.refresh(bucket)
        return bucket

    def delete_bucket(self, db: Session, bucket_id: str) -> bool:
        bucket = self.get_bucket(db, bucket_id)
        if bucket is None:
            return False
        db.delete(bucket)  # items cascade (FK ondelete + relationship cascade)
        db.commit()
        return True

    # ── item operations ─────────────────────────────────────────────────────────

    def add_item(
        self,
        db: Session,
        bucket_id: str,
        *,
        album_id: str,
        note: Optional[str] = None,
        today: Optional[date] = None,
    ) -> ReviewBucketItem:
        """Queue an album into a bucket, seeding its position from the recency+
        popularity score. The new item is inserted above existing items whose
        live score is lower, shifting them down (idempotent positions 0..n)."""
        bucket = self.get_bucket(db, bucket_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)

        album = db.query(Album).filter(Album.id == album_id).first()
        if album is None:
            raise AlbumNotFoundError(album_id)

        existing = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket_id)
            .order_by(ReviewBucketItem.position)
            .all()
        )
        if any(str(it.album_id) == str(album_id) for it in existing):
            raise DuplicateItemError(album_id)

        today = today or date.today()
        new_score, rec_reason = self._score(album, today=today)

        # Insertion index = count of existing items whose live score ranks at or
        # above the newcomer. Existing items keep their relative order; only the
        # newcomer is placed by score (manual reorder later overrides everything).
        insert_idx = 0
        for it in existing:
            it_score, _ = self._score(it.album, today=today)
            if it_score >= new_score:
                insert_idx += 1
            else:
                break

        item = ReviewBucketItem(
            bucket_id=bucket.id,
            album_id=album_id,
            note=note,
            rec_reason=rec_reason,
            position=insert_idx,
        )
        # Renumber the whole column 0..n from the final ordering so positions
        # stay dense and idempotent (the newcomer shifts later items down by one).
        final_order = existing[:insert_idx] + [item] + existing[insert_idx:]
        for pos, it in enumerate(final_order):
            it.position = pos

        db.add(item)
        db.commit()
        db.refresh(item)
        return item

    def update_item(
        self,
        db: Session,
        bucket_id: str,
        item_id: str,
        *,
        note: Optional[str] = None,
        status: Optional[str] = None,
        post_id: Optional[str] = None,
    ) -> ReviewBucketItem:
        item = (
            db.query(ReviewBucketItem)
            .filter(
                ReviewBucketItem.id == item_id,
                ReviewBucketItem.bucket_id == bucket_id,
            )
            .first()
        )
        if item is None:
            raise ItemNotFoundError(item_id)
        if note is not None:
            item.note = note or None
        if status is not None:
            item.status = status
        if post_id is not None:
            item.post_id = post_id or None
        db.commit()
        db.refresh(item)
        return item

    def delete_item(self, db: Session, bucket_id: str, item_id: str) -> bool:
        item = (
            db.query(ReviewBucketItem)
            .filter(
                ReviewBucketItem.id == item_id,
                ReviewBucketItem.bucket_id == bucket_id,
            )
            .first()
        )
        if item is None:
            return False
        db.delete(item)
        db.commit()
        return True

    # ── drag-and-drop persistence ───────────────────────────────────────────────

    def reorder(self, db: Session, buckets: List[dict]) -> None:
        """Idempotently re-apply a drag result.

        Payload: ``[{id, item_ids:[...]}, ...]`` — for each listed bucket, the
        item_ids in their new top→bottom order. An item appearing under a bucket
        other than its current one is a cross-bucket move (bucket_id reassigned).
        Positions are rewritten 0..n so repeated calls converge.
        """
        # Validate target buckets up front.
        bucket_ids = [b["id"] for b in buckets]
        found = {
            str(b.id)
            for b in db.query(ReviewBucket.id)
            .filter(ReviewBucket.id.in_(bucket_ids))
            .all()
        }
        missing = [bid for bid in bucket_ids if str(bid) not in found]
        if missing:
            raise BucketNotFoundError(missing[0])

        # Collect every referenced item id and load in one query.
        all_item_ids = [iid for b in buckets for iid in b.get("item_ids", [])]
        items_by_id = {
            str(it.id): it
            for it in db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.id.in_(all_item_ids))
            .all()
        }
        unknown = [iid for iid in all_item_ids if str(iid) not in items_by_id]
        if unknown:
            raise ItemNotFoundError(unknown[0])

        for b in buckets:
            for pos, item_id in enumerate(b.get("item_ids", [])):
                item = items_by_id[str(item_id)]
                item.bucket_id = b["id"]
                item.position = pos

        db.commit()
