"""Member-scoped tracked-artist persistence for personal release tracking."""
from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from myblog_shared_db.models import (
    Artist,
    UserArtistFollowExclusion,
    UserArtistTrack,
    UserArtistTrackOrigin,
)


class ArtistNotFoundError(Exception):
    """At least one requested catalog artist does not exist."""


class TrackedArtistRateLimitError(Exception):
    """The member's rolling-24h tracked-artist creation cap was exceeded."""


# V58 provenance values. An edge is the UNION of its origins (OQ2): a member can
# arrive at an artist by adding them here or by following them on Spotify, and those
# are different facts with different removal semantics. This service owns exactly one
# of them — 'manual' — and must never create or delete the worker's 'spotify_follow'
# row, or a site action would silently undo a provider fact.
MANUAL_ORIGIN = "manual"
SPOTIFY_FOLLOW_ORIGIN = "spotify_follow"


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
        # RETURNING (only actually-inserted rows come back) instead of
        # rowcount: prod psycopg reported rowcount=-1 for this statement,
        # yielding added=-1 / already_tracked=len+1 in the live response.
        result = db.execute(
            pg_insert(UserArtistTrack)
            .values(values)
            .on_conflict_do_nothing(index_elements=["user_id", "artist_id"])
            .returning(UserArtistTrack.artist_id)
        )
        added = len(result.scalars().all())

        # Provenance for every requested artist, not only the newly created edges: an
        # artist the member already tracks through a Spotify follow gains a 'manual'
        # origin when they add it by hand, which is what makes the later unfollow leave
        # the edge standing. ON CONFLICT DO NOTHING makes the re-add idempotent.
        db.execute(
            pg_insert(UserArtistTrackOrigin)
            .values([
                {"user_id": user_id, "artist_id": artist_id, "origin": MANUAL_ORIGIN}
                for artist_id in unique_ids
            ])
            .on_conflict_do_nothing(
                index_elements=["user_id", "artist_id", "origin"]
            )
        )

        # An explicit add is the "until cleared" of OQ2. Leaving the exclusion in place
        # would make the site's own add button a no-op for exactly the artists the
        # member had previously removed — the fence is against automatic re-import, not
        # against the member changing their mind.
        db.execute(
            delete(UserArtistFollowExclusion).where(
                UserArtistFollowExclusion.user_id == user_id,
                UserArtistFollowExclusion.artist_id.in_(unique_ids),
            )
        )
        db.commit()
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
        """Remove the member's tracked edge and record an exclusion (OQ2).

        The exclusion is the point, and it is written in the SAME transaction as the
        delete. From Step 5 the worker reconciles Spotify follows into this table every
        15 minutes, so without the exclusion a removal here is a *pause*: the provider
        still reports the follow on the next pass and the edge comes straight back,
        with the member's only escape being to unfollow on Spotify — a place we
        deliberately never write to. Between a committed delete and a separate
        exclusion write there is a window where exactly that resurrection can happen,
        which is why the two are one transaction and a failure rolls both back.

        The whole edge goes, not just its manual origin: removing an artist here is the
        member's strongest available statement about that artist, and an exclusion
        already suppresses the Spotify origin, so leaving a 'spotify_follow' row behind
        would keep an edge alive that nothing is allowed to refresh. Provenance rows
        cascade with the edge (V58's composite FK).
        """
        row = db.execute(
            select(UserArtistTrack).where(
                UserArtistTrack.user_id == user_id,
                UserArtistTrack.artist_id == artist_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        db.delete(row)
        db.flush()
        db.execute(
            pg_insert(UserArtistFollowExclusion)
            .values(user_id=user_id, artist_id=artist_id)
            .on_conflict_do_nothing(index_elements=["user_id", "artist_id"])
        )
        db.commit()
        return True
