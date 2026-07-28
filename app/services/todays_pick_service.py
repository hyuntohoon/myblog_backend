# app/services/todays_pick_service.py
# FEAT-today-buckit Step 4 — owner-curated "song of the day" store.
#
# Backs the `TodaySongBuckit` home tile + its browsable history. One pick per
# calendar day (`pick_date` UNIQUE); re-POSTing the same day upserts over the
# existing row. The pick is 100% manual (no rotation) — no-pick days are
# intentionally empty and render nothing on the home.
#
# The public GETs are self-contained: the denormalized display columns
# (title / artist / cover_url / spotify_track_id) are written by the owner PUT
# and read back directly, with no cross-service join to musicApi at read time.
#
# Transaction boundary lives here (commit once per mutation), mirroring
# GenreService / BucketService. Single owner → no ownership checks; the route
# gates writes via `require_owner`.
from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.kst import kst_today
from myblog_shared_db.models import DailyPick, DailyPickQueue


class TodaysPickService:
    """Daily-pick store, public history, and private owner staging queue."""

    # ── reads (public — edge_guard only) ────────────────────────────────────

    def get_today(self, db: Session) -> Optional[DailyPick]:
        """Today's pick, or None on a no-pick day. The home tile hides on None.

        "Today" is the KST day (`app.core.kst`), not Postgres' `current_date` —
        the DB session runs in UTC, so `current_date` rolled the pick over at
        09:00 KST instead of midnight (A-4).
        """
        return db.execute(
            select(DailyPick).where(DailyPick.pick_date == kst_today())
        ).scalar_one_or_none()

    def list_history(
        self,
        db: Session,
        *,
        limit: int = 30,
        before: Optional[date] = None,
    ) -> List[DailyPick]:
        """Date-desc history of past picks. `before` (exclusive upper bound)
        pages older entries; the route caps `limit` to [1, 100]."""
        stmt = select(DailyPick).order_by(DailyPick.pick_date.desc())
        if before is not None:
            stmt = stmt.where(DailyPick.pick_date < before)
        stmt = stmt.limit(limit)
        return list(db.execute(stmt).scalars().all())

    def list_queue(self, db: Session) -> List[DailyPickQueue]:
        """Owner-only staging queue, newest row first."""
        stmt = select(DailyPickQueue).order_by(DailyPickQueue.created_at.desc())
        return list(db.execute(stmt).scalars().all())

    # ── writes (owner — require_owner) ──────────────────────────────────────

    def upsert(
        self,
        db: Session,
        *,
        track_id: str,
        album_id: str,
        title: str,
        artist: str,
        cover_url: Optional[str],
        spotify_track_id: str,
    ) -> DailyPick:
        """Set today's pick. Upserts on `pick_date = today (KST)` — re-POSTing
        the same day overwrites the prior pick (the UNIQUE(pick_date) key). The
        server pins `pick_date` to today; the owner body carries no date."""
        stmt = self._daily_pick_upsert_stmt(
            track_id=track_id,
            album_id=album_id,
            title=title,
            artist=artist,
            cover_url=cover_url,
            spotify_track_id=spotify_track_id,
        )
        result = db.execute(stmt)
        row = result.scalar_one()
        db.commit()
        db.refresh(row)
        return row

    def add_to_queue(
        self,
        db: Session,
        *,
        track_id: str,
        album_id: str,
        title: str,
        artist: str,
        cover_url: Optional[str],
        spotify_track_id: str,
    ) -> DailyPickQueue:
        """Stage a track once. Re-adding its track_id returns the existing row."""
        stmt = (
            pg_insert(DailyPickQueue)
            .values(
                track_id=track_id,
                album_id=album_id,
                title=title,
                artist=artist,
                cover_url=cover_url,
                spotify_track_id=spotify_track_id,
            )
            .on_conflict_do_nothing(constraint="uq_daily_pick_queue_track")
            .returning(DailyPickQueue)
        )
        row = db.execute(stmt).scalar_one_or_none()
        if row is None:
            row = db.execute(
                select(DailyPickQueue).where(DailyPickQueue.track_id == track_id)
            ).scalar_one()
        db.commit()
        db.refresh(row)
        return row

    def delete_from_queue(self, db: Session, queue_id: str) -> bool:
        """Delete one staged pick. Returns False when the id does not exist."""
        row = db.get(DailyPickQueue, queue_id)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True

    def promote_from_queue(
        self, db: Session, queue_id: str
    ) -> Optional[DailyPick]:
        """Post a staged row as today's pick and consume it atomically."""
        queue_row = db.execute(
            select(DailyPickQueue)
            .where(DailyPickQueue.id == queue_id)
            .with_for_update()
        ).scalar_one_or_none()
        if queue_row is None:
            return None

        try:
            pick = db.execute(
                self._daily_pick_upsert_stmt(
                    track_id=str(queue_row.track_id),
                    album_id=str(queue_row.album_id),
                    title=queue_row.title,
                    artist=queue_row.artist,
                    cover_url=queue_row.cover_url,
                    spotify_track_id=queue_row.spotify_track_id,
                )
            ).scalar_one()
            db.delete(queue_row)
            db.commit()
        except Exception:
            db.rollback()
            raise

        db.refresh(pick)
        return pick

    @staticmethod
    def _daily_pick_upsert_stmt(
        *,
        track_id: str,
        album_id: str,
        title: str,
        artist: str,
        cover_url: Optional[str],
        spotify_track_id: str,
    ):
        """Build the shared daily-pick upsert without owning a transaction.

        `pick_date` is the KST day computed in Python, matching `get_today()`.
        Both halves had to move together: with the write on UTC `current_date`
        and the read on KST (or vice versa) a pick posted between 00:00 and
        09:00 KST would be stored under one date and looked up under another.
        """
        values = {
            "pick_date": kst_today(),
            "track_id": track_id,
            "album_id": album_id,
            "title": title,
            "artist": artist,
            "cover_url": cover_url,
            "spotify_track_id": spotify_track_id,
            "updated_at": func.now(),
        }
        stmt = (
            pg_insert(DailyPick)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_daily_picks_pick_date",
                set_={
                    "track_id": track_id,
                    "album_id": album_id,
                    "title": title,
                    "artist": artist,
                    "cover_url": cover_url,
                    "spotify_track_id": spotify_track_id,
                    "updated_at": func.now(),
                },
            )
            .returning(DailyPick)
        )
        return stmt

    def delete_today(self, db: Session) -> bool:
        """Clear today's pick ("unpost today"). Returns True iff a row was
        deleted; the route maps False → 404 (nothing posted today)."""
        row = self.get_today(db)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
