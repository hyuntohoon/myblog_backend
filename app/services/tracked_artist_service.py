"""Member-scoped tracked-artist persistence for personal release tracking."""
from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from myblog_shared_db.models import Artist, UserArtistTrack


class ArtistNotFoundError(Exception):
    """At least one requested catalog artist does not exist."""


class TrackedArtistRateLimitError(Exception):
    """The member's rolling-24h tracked-artist creation cap was exceeded."""


class TrackedArtistService:
    """CRUD for the per-member ``user_artist_tracks`` edge."""

    def tracked_artist_ids(
        self, db: Session, user_id: uuid.UUID, artist_ids: Iterable[uuid.UUID]
    ) -> set[uuid.UUID]:
        ids = set(artist_ids)
        if not ids:
            return set()
        rows = db.execute(
            select(UserArtistTrack.artist_id).where(
                UserArtistTrack.user_id == user_id,
                UserArtistTrack.artist_id.in_(ids),
            )
        ).scalars()
        return set(rows)

    def add_tracks(
        self,
        db: Session,
        user_id: uuid.UUID,
        artist_ids: Iterable[uuid.UUID],
        *,
        daily_cap: int | None = None,
    ) -> tuple[int, int]:
        """Validate and bulk-upsert distinct artist ids for one member.

        Values are sorted by the full conflict key before the INSERT to keep
        concurrent SQS/API batches from acquiring unique-index locks in different
        orders. Unknown artist ids reject the whole request before any write.
        """
        unique_ids = sorted(set(artist_ids), key=str)
        if not unique_ids:
            return 0, 0

        existing_artists = set(
            db.execute(
                select(Artist.id).where(Artist.id.in_(unique_ids))
            ).scalars()
        )
        unknown_ids = [
            artist_id
            for artist_id in unique_ids
            if artist_id not in existing_artists
        ]
        if unknown_ids:
            raise ArtistNotFoundError(str(unknown_ids[0]))

        already_ids = self.tracked_artist_ids(db, user_id, unique_ids)
        rows_to_create = len(unique_ids) - len(already_ids)
        if daily_cap is not None and rows_to_create:
            recent = db.scalar(
                select(func.count())
                .select_from(UserArtistTrack)
                .where(
                    UserArtistTrack.user_id == user_id,
                    UserArtistTrack.added_at
                    >= func.now() - text("interval '24 hours'"),
                )
            )
            recent_count = int(recent or 0)
            if recent_count + rows_to_create > daily_cap:
                raise TrackedArtistRateLimitError(
                    f"{recent_count}+{rows_to_create}/{daily_cap} in 24h"
                )

        # Sort by (user_id, artist_id), the UNIQUE conflict key. user_id is fixed
        # within this member-scoped request, but is included explicitly for clarity.
        values = sorted(
            (
                {"user_id": user_id, "artist_id": artist_id}
                for artist_id in unique_ids
            ),
            key=lambda row: (str(row["user_id"]), str(row["artist_id"])),
        )
        result = db.execute(
            pg_insert(UserArtistTrack)
            .values(values)
            .on_conflict_do_nothing(index_elements=["user_id", "artist_id"])
        )
        db.commit()
        added = int(result.rowcount or 0)
        return added, len(unique_ids) - added

    def list_tracks(self, db: Session, user_id: uuid.UUID):
        return db.execute(
            select(UserArtistTrack, Artist)
            .join(Artist, Artist.id == UserArtistTrack.artist_id)
            .where(UserArtistTrack.user_id == user_id)
            .order_by(UserArtistTrack.added_at.desc(), Artist.name, Artist.id)
        ).all()

    def delete_track(
        self, db: Session, user_id: uuid.UUID, artist_id: uuid.UUID
    ) -> bool:
        row = db.execute(
            select(UserArtistTrack).where(
                UserArtistTrack.user_id == user_id,
                UserArtistTrack.artist_id == artist_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
