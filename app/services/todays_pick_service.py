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

from myblog_shared_db.models import DailyPick


class TodaysPickService:
    """Owner-writable daily pick store + public history (FEAT-today-buckit)."""

    # ── reads (public — edge_guard only) ────────────────────────────────────

    def get_today(self, db: Session) -> Optional[DailyPick]:
        """Today's pick, or None on a no-pick day. The home tile hides on None."""
        return db.execute(
            select(DailyPick).where(DailyPick.pick_date == func.current_date())
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
        """Set today's pick. Upserts on `pick_date = current_date` — re-POSTing
        the same day overwrites the prior pick (the UNIQUE(pick_date) key). The
        server pins `pick_date` to today; the owner body carries no date."""
        values = {
            "pick_date": func.current_date(),
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
        result = db.execute(stmt)
        row = result.scalar_one()
        db.commit()
        db.refresh(row)
        return row

    def delete_today(self, db: Session) -> bool:
        """Clear today's pick ("unpost today"). Returns True iff a row was
        deleted; the route maps False → 404 (nothing posted today)."""
        row = self.get_today(db)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
