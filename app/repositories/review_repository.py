# app/repositories/review_repository.py
from __future__ import annotations

from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from myblog_shared_db.models import PostReview, Track


class ReviewRepository:
    """post_reviews 테이블 전용 리포지토리.

    트랙 평점은 (post_id, track_id) partial UNIQUE 인덱스 위에서 UPSERT.
    앨범 평점은 별도 — 본 RFC scope 에서는 posts.rating (legacy) 만 사용하며
    subject='album' 쓰기는 노출하지 않는다.
    """

    def list_track_reviews(self, db: Session, post_id: str) -> List[PostReview]:
        return (
            db.query(PostReview)
            .filter(PostReview.post_id == post_id, PostReview.subject == "track")
            .order_by(PostReview.created_at.asc())
            .all()
        )

    def get_track_review(
        self, db: Session, post_id: str, track_id: str
    ) -> Optional[PostReview]:
        return (
            db.query(PostReview)
            .filter(
                PostReview.post_id == post_id,
                PostReview.track_id == track_id,
                PostReview.subject == "track",
            )
            .first()
        )

    def existing_track_ids(
        self, db: Session, track_ids: Sequence[str]
    ) -> set[str]:
        if not track_ids:
            return set()
        rows = db.execute(
            select(Track.id).where(Track.id.in_(list(track_ids)))
        ).all()
        return {str(r[0]) for r in rows}

    def upsert_track_review(
        self,
        db: Session,
        *,
        post_id: str,
        track_id: str,
        rating: float,
        scale: int,
        notes: Optional[str],
    ) -> PostReview:
        """Upsert against the partial UNIQUE index uq_post_reviews_post_track."""
        stmt = (
            pg_insert(PostReview)
            .values(
                post_id=post_id,
                subject="track",
                album_id=None,
                track_id=track_id,
                rating_value=rating,
                rating_scale=scale,
                notes=notes,
            )
            .on_conflict_do_update(
                index_elements=["post_id", "track_id"],
                index_where=text("track_id IS NOT NULL"),
                set_={
                    "rating_value": rating,
                    "rating_scale": scale,
                    "notes": notes,
                },
            )
            .returning(PostReview.id)
        )
        db.execute(stmt)
        return self.get_track_review(db, post_id, track_id)  # type: ignore[return-value]

    def delete_track_review(
        self, db: Session, post_id: str, track_id: str
    ) -> bool:
        row = self.get_track_review(db, post_id, track_id)
        if not row:
            return False
        db.delete(row)
        return True

    def batch_upsert_track_reviews(
        self,
        db: Session,
        *,
        post_id: str,
        items: Sequence[dict],
    ) -> None:
        """All-or-nothing: callers manage the surrounding transaction.

        Each item must carry track_id, rating, scale, notes.
        """
        if not items:
            return
        values = [
            {
                "post_id": post_id,
                "subject": "track",
                "album_id": None,
                "track_id": it["track_id"],
                "rating_value": it["rating"],
                "rating_scale": it["scale"],
                "notes": it.get("notes"),
            }
            for it in items
        ]
        stmt = pg_insert(PostReview).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["post_id", "track_id"],
            index_where=text("track_id IS NOT NULL"),
            set_={
                "rating_value": stmt.excluded.rating_value,
                "rating_scale": stmt.excluded.rating_scale,
                "notes": stmt.excluded.notes,
            },
        )
        db.execute(stmt)
