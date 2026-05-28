# app/services/review_service.py
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.repositories.post_repository import PostRepository
from app.repositories.review_repository import ReviewRepository


class PostNotFoundError(Exception):
    """Raised when the target post does not exist."""


class InvalidTrackError(Exception):
    """Raised when a referenced track_id does not exist in the tracks table.

    Carries the offending id so the API layer can echo it in the 4xx detail.
    """

    def __init__(self, track_id: str):
        super().__init__(f"Track {track_id} not found")
        self.track_id = track_id


class ReviewService:
    """Bundles `posts.rating` (album, legacy) + `post_reviews` (per-track) reads,
    and gates per-track upsert / delete / batch writes behind track-existence
    validation. Batch writes are all-or-nothing (single transaction).
    """

    def __init__(
        self,
        review_repo: ReviewRepository,
        post_repo: PostRepository,
    ):
        self.review_repo = review_repo
        self.post_repo = post_repo

    def get_bundle(self, db: Session, post_id: str) -> dict:
        post = self.post_repo.get_by_id(db, post_id)
        if not post:
            raise PostNotFoundError(post_id)

        album = None
        if post.rating is not None:
            album = {"rating": float(post.rating), "scale": int(post.rating_scale)}

        tracks = [
            {
                "track_id": str(r.track_id),
                "rating": float(r.rating_value) if r.rating_value is not None else 0.0,
                "scale": int(r.rating_scale),
                "notes": r.notes,
            }
            for r in self.review_repo.list_track_reviews(db, post_id)
            if r.track_id is not None
        ]
        return {"album": album, "tracks": tracks}

    def upsert_track_review(
        self,
        db: Session,
        *,
        post_id: str,
        track_id: str,
        rating: float,
        scale: int,
        notes: Optional[str],
    ) -> dict:
        if not self.post_repo.get_by_id(db, post_id):
            raise PostNotFoundError(post_id)

        existing = self.review_repo.existing_track_ids(db, [track_id])
        if track_id not in existing:
            raise InvalidTrackError(track_id)

        row = self.review_repo.upsert_track_review(
            db,
            post_id=post_id,
            track_id=track_id,
            rating=rating,
            scale=scale,
            notes=notes,
        )
        db.commit()
        return {
            "track_id": str(row.track_id),
            "rating": float(row.rating_value) if row.rating_value is not None else 0.0,
            "scale": int(row.rating_scale),
            "notes": row.notes,
        }

    def delete_track_review(
        self, db: Session, post_id: str, track_id: str
    ) -> bool:
        if not self.post_repo.get_by_id(db, post_id):
            raise PostNotFoundError(post_id)
        deleted = self.review_repo.delete_track_review(db, post_id, track_id)
        if deleted:
            db.commit()
        return deleted

    def batch_upsert_track_reviews(
        self,
        db: Session,
        *,
        post_id: str,
        items: Sequence[dict],
    ) -> list[dict]:
        """All-or-nothing: validates every track_id up front, then commits once.
        Pre-validation guarantees the IntegrityError path stays cold for the
        documented failure mode; any IntegrityError surfaced here is a real
        DB-level bug worth surfacing.
        """
        if not self.post_repo.get_by_id(db, post_id):
            raise PostNotFoundError(post_id)

        track_ids = [it["track_id"] for it in items]
        existing = self.review_repo.existing_track_ids(db, track_ids)
        for tid in track_ids:
            if tid not in existing:
                raise InvalidTrackError(tid)

        try:
            self.review_repo.batch_upsert_track_reviews(
                db, post_id=post_id, items=list(items)
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

        return [
            {
                "track_id": str(r.track_id),
                "rating": float(r.rating_value) if r.rating_value is not None else 0.0,
                "scale": int(r.rating_scale),
                "notes": r.notes,
            }
            for r in self.review_repo.list_track_reviews(db, post_id)
            if r.track_id is not None and str(r.track_id) in set(track_ids)
        ]
