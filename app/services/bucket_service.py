# app/services/bucket_service.py
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from myblog_shared_db.models import (
    Album,
    Artist,
    BucketItemSnapshot,
    Post,
    ReviewBucket,
    ReviewBucketItem,
    SpotifyLibraryAlbum,
    Track,
    User,
    post_albums_table as post_albums,
)

from app.services.distribution import VARIOUS_ARTISTS

# Sentinel for "argument not provided" — lets update_bucket() tell an omitted
# field apart from an explicit None. The route strips unsent fields via
# model_dump(exclude_unset=True), so a color=None reaching the service means the
# client deliberately sent {"color": null} to reset the bucket to the default ink.
_UNSET: Any = object()


class BucketNotFoundError(Exception):
    """Raised when a bucket id does not exist. Route maps to 404."""


class ItemNotFoundError(Exception):
    """Raised when a bucket item id does not exist. Route maps to 404."""


class AlbumNotFoundError(Exception):
    """Raised when an album id does not exist. Route maps to 404."""


class DuplicateItemError(Exception):
    """Raised when a de-duplicated kind (album/track/review) is already in the target bucket.
    Route maps to 409.

    Distinct from `already_reviewed` (a non-blocking advisory badge): this guards the per-kind
    partial-uniques (V30) — uq_review_bucket_items_{album,track,review} — so the same
    album / track / reviewed-post can't sit twice in one bucket. playback/snapshot allow
    duplicates (D8)."""


class TrackNotFoundError(Exception):
    """Raised when a track_id does not exist (track / playback membership). Route maps to 404."""


class ReviewTargetNotFoundError(Exception):
    """Raised when a review_target_id (a posts.id) does not exist for a review membership.
    Route maps to 404. DISTINCT from post_id (= the review an album produced)."""


class ArtistNotFoundError(Exception):
    """Raised when an artist_id (artist / source-expansion member) does not exist. Route maps
    to 404. FEAT-my-buckit-artist (V32)."""


class BucketTypeError(Exception):
    """Raised when a write violates a bucket's type discriminator — e.g. a non-artist item
    dropped on an Artist bucket, or an attempt to change a bucket's immutable type. Route maps
    to 400. FEAT-my-buckit-artist (V32)."""


class SystemBucketError(Exception):
    """Raised when a write targets a SYSTEM-owned bucket in a way only user buckets allow —
    today, deleting one. Route maps to 409. FEAT-playback-bucket-player Step 3.

    Note this guard did not exist before: `delete_bucket` checked ownership only, so the two
    pre-existing system buckets (spotify_library / to_listen) were deletable by their owner
    even though nothing in the product offers that action. The playback queue made the gap
    worth closing for all three kinds at once rather than for the new one alone."""


class BucketRateLimitError(Exception):
    """Per-member daily bucket create cap hit. Route maps to 429."""


class BucketItemRateLimitError(Exception):
    """Per-member daily bucket-item create cap hit. Route maps to 429."""


class GrowPostNotFoundError(Exception):
    """Raised when nightly-grow references a post id that does not exist. Route maps to 404.
    FIX-nightly-draft-identity."""


class GrowPostNotDraftError(Exception):
    """Raised when nightly-grow references a post that is not status='draft'. Route maps to
    409 — the agent may only link the draft it just created, never publish-era editorial
    state. FIX-nightly-draft-identity."""


# Auto-recommendation: initial position is seeded from a weighted blend of two
# DB-only signals (RFC §"백엔드 API" — release recency + Spotify popularity).
# After the first placement, manual drag (PUT /reorder) is the source of truth.
W_RECENCY = 0.6
W_POPULARITY = 0.4
# Linear recency decay window: an album older than this scores 0 on recency.
RECENCY_WINDOW_DAYS = 365 * 2

# FEAT-spotify-library-sync: the single special bucket mirroring the owner's Spotify
# saved-albums Library. Get-or-create lives in the BACKEND (the worker creates none).
SPOTIFY_LIBRARY_BUCKET_KIND = "spotify_library"
SPOTIFY_LIBRARY_BUCKET_NAME = "Spotify 라이브러리"
# Server-side debounce: ignore a sync POST if one ran within this window (derived
# from max(spotify_library_albums.last_synced_at)). ~30s per the interface spec.
LIBRARY_SYNC_DEBOUNCE_SECONDS = 30

# FEAT-playback-bucket-player (V51): the per-user Playback Bucket — the system-owned bucket
# whose ordered item list IS the playback queue. `kind` marks it system-owned (the free-TEXT
# role axis, matching spotify_library); `type` is the third value of the closed discriminator
# enum (ck_review_buckets_type), so the type gate can reject non-queue drops.
PLAYBACK_BUCKET_KIND = "playback_queue"
PLAYBACK_BUCKET_TYPE = "playback"
PLAYBACK_BUCKET_NAME = "재생 대기열"

# Every SYSTEM-owned bucket kind. These are seeded by the server, not by create_bucket, and
# the product offers no delete action for any of them — see SystemBucketError for why the
# guard covers all three rather than only the new one.
SYSTEM_BUCKET_KINDS = ("playback_queue", "spotify_library", "to_listen")


class BucketService:
    """Review-queue kanban: user-created buckets holding queued albums.

    FEAT-multi-user Phase 2: every private read/write is scoped to a user_id (the
    acting member, provisioned at the route). Ownership is enforced by scoping the
    bucket/item lookups — a member can only see/mutate their own buckets. The
    public viewer (list_public_buckets) stays cross-user. Transaction boundary
    lives in the service (commit once per mutation), mirroring PostService.
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

    def list_buckets(self, db: Session, user_id: uuid.UUID) -> List[ReviewBucket]:
        """Nested tree of buckets. Returns only ROOT buckets (parent_id IS NULL);
        each node carries its descendants on a transient ``children_nodes`` list
        (recursive), every sibling level ordered by (position, created_at). Items
        stay populated per bucket via the relationship's order_by.

        ``children_nodes`` is a non-column attribute attached for serialization —
        it does not exist on the ORM model and is never persisted.
        """
        # Eager-load the item briefs in a fixed number of queries (one extra SELECT
        # per relationship level via selectinload) instead of a 2-level N+1 that fired
        # item.album + album.artists (+ track + track.artists) lazily PER board item.
        all_buckets = (
            db.query(ReviewBucket)
            .options(
                selectinload(ReviewBucket.items)
                .selectinload(ReviewBucketItem.album)
                .selectinload(Album.artists),
                selectinload(ReviewBucket.items)
                .selectinload(ReviewBucketItem.track)
                .selectinload(Track.artists),
                # FEAT-my-buckit-artist (V32): artist briefs for artist-kind rows (one extra
                # SELECT, same idiom — avoids a lazy item.artist per Artist Buckit member).
                selectinload(ReviewBucket.items).selectinload(ReviewBucketItem.artist),
            )
            .filter(ReviewBucket.user_id == user_id)
            .order_by(ReviewBucket.position, ReviewBucket.created_at)
            .all()
        )
        # Group children by parent_id (None key = roots). The query order is
        # already (position, created_at), so each group preserves sibling order.
        children_by_parent: dict = {}
        for b in all_buckets:
            children_by_parent.setdefault(
                str(b.parent_id) if b.parent_id is not None else None, []
            ).append(b)

        def _attach(node: ReviewBucket) -> ReviewBucket:
            node.children_nodes = [
                _attach(child)
                for child in children_by_parent.get(str(node.id), [])
            ]
            return node

        return [_attach(root) for root in children_by_parent.get(None, [])]

    # ── tree movement ─────────────────────────────────────────────────────────

    def _walk_ancestors(self, db: Session, bucket_id: str):
        """Yield ancestor ids of ``bucket_id`` walking parent_id upward to root.

        No ORM relationship exists for parent_id, so we query id→parent_id in a
        loop. Bounded by ``_MAX_TREE_DEPTH`` so a corrupt cycle in the data can't
        spin forever.
        """
        current = bucket_id
        for _ in range(self._MAX_TREE_DEPTH):
            row = (
                db.query(ReviewBucket.parent_id)
                .filter(ReviewBucket.id == current)
                .first()
            )
            if row is None or row[0] is None:
                return
            parent = str(row[0])
            yield parent
            current = parent

    _MAX_TREE_DEPTH = 1000

    def move_bucket(
        self,
        db: Session,
        bucket_id: str,
        parent_id: Optional[str],
        position: int,
        user_id: uuid.UUID,
    ) -> ReviewBucket:
        """Reparent ``bucket_id`` under ``parent_id`` (None => root) at ``position``.

        Cycle prevention: 400 (ValueError) if parent_id == bucket_id, or if
        bucket_id is an ancestor of parent_id (moving a node under its own
        descendant). 404 (BucketNotFoundError) if bucket or parent is missing.
        After reparenting, siblings sharing the new parent_id are renumbered so
        the moved bucket lands at ``position`` and positions are contiguous 0..n.
        """
        bucket = self.get_bucket(db, bucket_id, user_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)

        if parent_id is not None:
            parent = self.get_bucket(db, parent_id, user_id)
            if parent is None:
                raise BucketNotFoundError(parent_id)
            if str(parent_id) == str(bucket_id):
                raise ValueError("a bucket cannot be its own parent")
            # Reject if bucket_id is an ancestor of parent_id: walking up from
            # the new parent must not encounter the bucket we're moving.
            if any(
                anc == str(bucket_id)
                for anc in self._walk_ancestors(db, parent_id)
            ):
                raise ValueError("cannot move a bucket under its own descendant")

        new_parent = str(parent_id) if parent_id is not None else None
        old_parent_id = bucket.parent_id  # capture before reparenting
        old_parent = str(old_parent_id) if old_parent_id is not None else None
        bucket.parent_id = parent_id

        # Renumber the destination sibling group (same parent_id). Exclude the
        # moved bucket from the ordered baseline, then splice it in at `position`.
        siblings = (
            db.query(ReviewBucket)
            .filter(
                ReviewBucket.parent_id.is_(None)
                if new_parent is None
                else ReviewBucket.parent_id == parent_id
            )
            .filter(ReviewBucket.user_id == user_id)
            .order_by(ReviewBucket.position, ReviewBucket.created_at)
            .all()
        )
        others = [s for s in siblings if str(s.id) != str(bucket_id)]
        idx = max(0, min(int(position), len(others)))
        ordered = others[:idx] + [bucket] + others[idx:]
        for pos, s in enumerate(ordered):
            s.position = pos

        # When the parent changed, compact the *old* parent's remaining siblings
        # so their positions stay contiguous 0..n (no gap left where the bucket
        # was). Filter the moved bucket out by id so its new position is kept
        # regardless of autoflush ordering.
        if old_parent != new_parent:
            old_siblings = (
                db.query(ReviewBucket)
                .filter(
                    ReviewBucket.parent_id.is_(None)
                    if old_parent is None
                    else ReviewBucket.parent_id == old_parent_id
                )
                .filter(ReviewBucket.user_id == user_id)
                .order_by(ReviewBucket.position, ReviewBucket.created_at)
                .all()
            )
            remaining = [s for s in old_siblings if str(s.id) != str(bucket_id)]
            for pos, s in enumerate(remaining):
                s.position = pos

        db.commit()
        db.refresh(bucket)
        return bucket

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
        self,
        db: Session,
        user_id: uuid.UUID,
        *,
        name: str,
        color: Optional[str] = None,
        type: str = "general",
        daily_cap: Optional[int] = None,
    ) -> ReviewBucket:
        name = (name or "").strip()
        if not name:
            raise ValueError("name required")
        # FEAT-my-buckit-artist (V32): the bucket-level type discriminator. create_bucket only
        # ever mints user buckets (kind defaults 'review'); system buckets (spotify_library /
        # to_listen / playback_queue) are seeded elsewhere. Guard the enum here for direct
        # callers (the request Literal already gates the route).
        #
        # FEAT-playback-bucket-player: 'playback' is a valid DB value (V51 widened
        # ck_review_buckets_type) but is deliberately NOT accepted from user input — the queue
        # is minted only by get_or_create_playback_bucket, which also sets kind='playback_queue'
        # and is covered by the per-user unique index. Letting a user create one here would
        # produce a type='playback' bucket with kind='review': a queue the singleton index does
        # not constrain and the delete guard does not protect.
        if type not in ("general", "artist"):
            raise ValueError("type must be general|artist")
        if daily_cap is not None:
            recent = db.scalar(
                select(func.count())
                .select_from(ReviewBucket)
                .where(
                    ReviewBucket.user_id == user_id,
                    ReviewBucket.created_at >= func.now() - text("interval '24 hours'"),
                )
            )
            if recent is not None and recent >= daily_cap:
                raise BucketRateLimitError(f"{recent}/{daily_cap} in 24h")
        next_pos = db.execute(
            select(func.coalesce(func.max(ReviewBucket.position), -1)).where(
                ReviewBucket.user_id == user_id
            )
        ).scalar_one()
        bucket = ReviewBucket(
            user_id=user_id, name=name, color=color, type=type, position=int(next_pos) + 1
        )
        db.add(bucket)
        db.commit()
        db.refresh(bucket)
        return bucket

    def get_bucket(
        self, db: Session, bucket_id: str, user_id: Optional[uuid.UUID] = None
    ) -> Optional[ReviewBucket]:
        # Single .filter(*conds) (not chained) so the ownership scope adds no extra
        # query-chain hop — keeps the mock-based unit tests' one-filter wiring valid.
        conds = [ReviewBucket.id == bucket_id]
        if user_id is not None:
            conds.append(ReviewBucket.user_id == user_id)
        return db.query(ReviewBucket).filter(*conds).first()

    def update_bucket(
        self,
        db: Session,
        user_id: uuid.UUID,
        bucket_id: str,
        *,
        name: Optional[str] = None,
        color: Any = _UNSET,
        position: Optional[int] = None,
        is_done: Optional[bool] = None,
        research_mode: Optional[str] = None,
        is_public: Optional[bool] = None,
        type: Any = _UNSET,
    ) -> ReviewBucket:
        bucket = self.get_bucket(db, bucket_id, user_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)
        # FEAT-my-buckit-artist (V32): bucket type is set once at create and immutable in v1.
        # UpdateBucketRequest omits `type` today, so this guard only fires for a direct service
        # caller (or a future field add) — a no-op same-type value passes, an actual change is a
        # 400. Pre-empts a silent General↔Artist flip that would orphan member-composition.
        if type is not _UNSET and type != bucket.type:
            raise BucketTypeError("bucket type is immutable")
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("name cannot be empty")
            bucket.name = name
        if color is not _UNSET:
            # None or empty string clears the color label (reset to default ink).
            bucket.color = color or None
        if position is not None:
            bucket.position = int(position)
        if research_mode is not None:
            # FEAT-album-research-notes: opt-in auto-research scope. Schema Literal
            # gates the request layer; guard here for direct service callers.
            if research_mode not in ("off", "all", "selected"):
                raise ValueError("research_mode must be off|all|selected")
            bucket.research_mode = research_mode
        if is_done is not None:
            # The partial unique index (idx_review_buckets_single_done) enforces
            # at-most-one done bucket; a second true surfaces as IntegrityError →
            # the route maps it to 409.
            bucket.is_done = bool(is_done)
        if is_public is not None:
            # FEAT-public-bucket-multiuser Scope A: opt-in public visibility. A SYSTEM bucket
            # must never be published — guard here so a direct PATCH can't expose the owner's
            # Spotify library, to-listen queue, or now-playing queue through the public viewer.
            # (list_public_buckets also filters kind='review', so this is the second of two
            # gates; it was previously spotify_library-only — same per-kind drift as the
            # missing delete guard, fixed in the same pass.)
            if bool(is_public) and getattr(bucket, "kind", None) in SYSTEM_BUCKET_KINDS:
                raise ValueError("a system bucket cannot be made public")
            bucket.is_public = bool(is_public)
        db.commit()
        db.refresh(bucket)
        return bucket

    def list_public_buckets(self, db: Session) -> List[Tuple[ReviewBucket, User]]:
        """Flat, position-ordered list of every member's published buckets
        (is_public=true) that are normal review columns (kind='review'), each
        paired with its owning user for public attribution.

        Post-P2 (FEAT-multi-user-accounts) buckets are per-user and ANY member
        can publish one via PATCH is_public — the public read therefore carries
        the owner so a member's shelf can never appear as anonymous, seemingly
        owner-curated content on /collection (the pre-P2 query had no user
        dimension at all).

        Deliberately FLAT (no nesting): exposing the parent/child tree would leak
        the existence/structure of private buckets via a published child. Each
        published bucket is a standalone public 'shelf'. The route projects these
        through the whitelisted Public* schemas (no private item fields; the owner
        is projected as handle/display_name only — both already public via
        /api/members).
        """
        return (
            db.query(ReviewBucket, User)
            .join(User, User.id == ReviewBucket.user_id)
            .options(
                selectinload(ReviewBucket.items)
                .selectinload(ReviewBucketItem.album)
                .selectinload(Album.artists),
                selectinload(ReviewBucket.items)
                .selectinload(ReviewBucketItem.track)
                .selectinload(Track.artists),
            )
            .filter(ReviewBucket.is_public.is_(True))
            .filter(ReviewBucket.kind == "review")
            .order_by(ReviewBucket.position, ReviewBucket.created_at)
            .all()
        )

    def delete_bucket(self, db: Session, user_id: uuid.UUID, bucket_id: str) -> bool:
        bucket = self.get_bucket(db, bucket_id, user_id)
        if bucket is None:
            return False
        # FEAT-playback-bucket-player Step 3 — pattern fix, not a new feature. System-owned
        # buckets are seeded by the server and have no delete affordance in the product; the
        # ownership check above was the ONLY gate, so a direct DELETE removed them (and
        # cascaded their items) for all three kinds. Rejected here so the guard cannot drift
        # per-kind. getattr default keeps partial unit-test mocks (no `kind`) deletable.
        if getattr(bucket, "kind", None) in SYSTEM_BUCKET_KINDS:
            raise SystemBucketError(
                f"'{bucket.kind}' is a system bucket and cannot be deleted"
            )
        # BUG-playback-system-bucket-cascade — the check above is NOT sufficient on its own.
        # `review_buckets.parent_id` is ON DELETE CASCADE, so deleting a user bucket takes its
        # whole subtree with it. Checking only the target's own kind therefore left the guard
        # trivially bypassable: nest a system bucket under a user crate (which the product
        # allows on purpose — system buckets are non-deletable but fully position-movable,
        # RFC T1) and delete that crate. Measured before the fix: direct DELETE → 409, but
        # nest-then-delete-parent → 204, and the Playback Bucket came back auto-created with a
        # new id and an empty queue. The realistic failure is not abuse — it is a member losing
        # a whole queue or Library to an ordinary 삭제 they thought applied to one crate.
        #
        # Enforced by walking UP from each system bucket rather than down from the target: the
        # user owns at most three, so this is ≤3 short ancestor walks and it reuses
        # `_walk_ancestors` (the same primitive move_bucket's cycle guard uses) instead of
        # introducing a second, drift-prone tree traversal.
        blocked = self._system_bucket_in_subtree(db, user_id, bucket_id)
        if blocked is not None:
            raise SystemBucketError(
                f"this bucket contains the system bucket '{blocked}'; move it out first"
            )
        db.delete(bucket)  # items cascade (FK ondelete + relationship cascade)
        db.commit()
        return True

    def _system_bucket_in_subtree(
        self, db: Session, user_id: uuid.UUID, bucket_id: str
    ) -> Optional[str]:
        """The `kind` of a system bucket living under ``bucket_id`` (at any depth), or None.

        Returns the kind rather than a bool so the 409 can name what is in the way — "move it
        out first" is only actionable if the member knows which bucket to move.
        """
        system_buckets = (
            db.query(ReviewBucket.id, ReviewBucket.kind)
            .filter(
                ReviewBucket.user_id == user_id,
                ReviewBucket.kind.in_(SYSTEM_BUCKET_KINDS),
            )
            .all()
        )
        target = str(bucket_id)
        for sys_id, sys_kind in system_buckets:
            if str(sys_id) == target:
                # Defensive: the caller already rejected this, but a direct call must not
                # report "no system bucket below" for a system bucket itself.
                return sys_kind
            if any(anc == target for anc in self._walk_ancestors(db, str(sys_id))):
                return sys_kind
        return None

    # ── item operations ─────────────────────────────────────────────────────────

    def add_item(
        self,
        db: Session,
        user_id: uuid.UUID,
        bucket_id: str,
        *,
        item_type: str = "album",
        album_id: Optional[str] = None,
        track_id: Optional[str] = None,
        review_target_id: Optional[str] = None,
        artist_id: Optional[str] = None,
        note: Optional[str] = None,
        snapshot: Optional[Any] = None,
        today: Optional[date] = None,
        daily_cap: Optional[int] = None,
    ) -> ReviewBucketItem:
        """Add a typed membership to a bucket. FEAT-pocket-buckit: the STEP-2 relax (V30/V31)
        is live, so every kind is writable:

        - ``album``  — Album lookup + recency/popularity score seeding + per-kind dedup
          (uq_review_bucket_items_album). The original path, behaviour unchanged.
        - ``track`` / ``playback`` — Track lookup; appended at the end. 'track' de-dupes
          (uq_..._track); 'playback' (queue) allows duplicates (D8).
        - ``review`` — Post lookup (the reviewed post = review_target_id, DISTINCT from the
          album-produced post_id); de-dupes (uq_..._review).
        - ``snapshot`` — append-only capture: a 'snapshot' membership row + one
          bucket_item_snapshots side-row frozen from ``snapshot`` (never an UPDATE; a refresh
          is a new row). Allows duplicates (D8).
        - ``artist`` — Artist lookup (FEAT-my-buckit-artist V32); appended at the end; de-dupes
          (uq_..._artist). The SOURCE-expansion variant (a featuring track / compilation album)
          is handled by :meth:`expand_artist_source`, not this single-row path.

        FEAT-my-buckit-artist type gate: an Artist bucket (``type='artist'``) accepts only
        ``item_type='artist'`` rows; any other kind is a 400 (BucketTypeError). A General bucket
        accepts every kind, including artist (today's behavior preserved).

        Raises BucketNotFoundError / AlbumNotFoundError / TrackNotFoundError /
        ReviewTargetNotFoundError / ArtistNotFoundError / BucketTypeError / DuplicateItemError,
        mapped to HTTP by the route."""
        bucket = self.get_bucket(db, bucket_id, user_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)

        self._assert_item_type_allowed(bucket, item_type)
        self._assert_manual_add_allowed(bucket)

        if item_type == "album":
            return self._add_album_item(
                db,
                bucket,
                user_id=user_id,
                album_id=album_id,
                note=note,
                today=today,
                daily_cap=daily_cap,
            )
        return self._add_typed_item(
            db,
            bucket,
            user_id=user_id,
            item_type=item_type,
            track_id=track_id,
            review_target_id=review_target_id,
            artist_id=artist_id,
            note=note,
            snapshot=snapshot,
            daily_cap=daily_cap,
        )

    @staticmethod
    def _assert_item_type_allowed(bucket: ReviewBucket, item_type: str) -> None:
        """The bucket-`type` membership gate, in ONE place so the single-row path and the
        source-expansion paths cannot drift apart.

        - ``artist`` bucket → artist rows only (FEAT-my-buckit-artist V32).
        - ``playback`` bucket → playback rows only (FEAT-playback-bucket-player). An artist
          drop is rejected outright; an ALBUM drop is rejected on this single-row path
          specifically because albums enter the queue through :meth:`expand_album_tracks`,
          which produces playback rows and therefore passes this same gate.
        - ``general`` bucket → every kind, unchanged.

        getattr default mirrors the serializer's defensive item_type read — a partial test
        mock / pre-V32 row reads as 'general' (no gate); the real ORM column is NOT NULL.
        """
        bucket_type = getattr(bucket, "type", "general")
        if bucket_type == "artist" and item_type != "artist":
            raise BucketTypeError(
                "this bucket only holds artists; non-artist items are rejected"
            )
        if bucket_type == PLAYBACK_BUCKET_TYPE and item_type != "playback":
            if item_type == "album":
                raise BucketTypeError(
                    "the playback queue holds tracks; drop an album to expand it into its "
                    "tracks (source_album_id) instead of adding the album itself"
                )
            raise BucketTypeError(
                "the playback queue only holds tracks; other items are rejected"
            )

    @staticmethod
    def _assert_manual_add_allowed(bucket: ReviewBucket) -> None:
        """BUG-20 follow-up: the frontend's `isManualAddTarget()` (lib/buckets.ts) was the
        ONLY thing stopping a manual add into the sync-owned `kind='spotify_library'` mirror
        bucket — this service had no matching gate, so a direct API call bypassed it entirely
        (`_assert_item_type_allowed` above keys on `bucket.type`, never `kind`). Mirrors the
        frontend predicate exactly: `spotify_library` only, NOT the other two
        `SYSTEM_BUCKET_KINDS` (`playback_queue` accepts manual queue-adds by design;
        `to_listen` isn't add-restricted, only delete-protected). The worker's own sync writes
        (library_sync_service.py) go straight to `review_bucket_items` via raw SQL, not this
        method, so they are unaffected.
        """
        if getattr(bucket, "kind", None) == SPOTIFY_LIBRARY_BUCKET_KIND:
            raise SystemBucketError(
                "spotify_library is sync-owned and cannot receive a manual add"
            )

    @staticmethod
    def _check_item_rate_limit(
        db: Session,
        user_id: uuid.UUID,
        daily_cap: Optional[int],
        *,
        rows_to_create: int = 1,
    ) -> None:
        """Reject a write that would put the member over the rolling-24h row cap."""
        if daily_cap is None or rows_to_create <= 0:
            return
        recent = db.scalar(
            select(func.count())
            .select_from(ReviewBucketItem)
            .join(ReviewBucket, ReviewBucketItem.bucket_id == ReviewBucket.id)
            .where(
                ReviewBucket.user_id == user_id,
                ReviewBucketItem.added_at >= func.now() - text("interval '24 hours'"),
            )
        )
        recent_count = int(recent or 0)
        if recent_count + rows_to_create > daily_cap:
            raise BucketItemRateLimitError(
                f"{recent_count}+{rows_to_create}/{daily_cap} in 24h"
            )

    def _add_album_item(
        self,
        db: Session,
        bucket: ReviewBucket,
        *,
        user_id: uuid.UUID,
        album_id: Optional[str],
        note: Optional[str],
        today: Optional[date],
        daily_cap: Optional[int],
    ) -> ReviewBucketItem:
        """The original album path: Album lookup + score seeding + per-kind dedup. Inserts the
        newcomer above existing items whose live score is lower, renumbering 0..n."""
        if not album_id:
            raise AlbumNotFoundError(album_id)
        album = db.query(Album).filter(Album.id == album_id).first()
        if album is None:
            raise AlbumNotFoundError(album_id)

        existing = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket.id)
            .order_by(ReviewBucketItem.position)
            .all()
        )
        if any(
            it.item_type == "album" and str(it.album_id) == str(album_id) for it in existing
        ):
            raise DuplicateItemError(album_id)

        today = today or date.today()
        new_score, rec_reason = self._score(album, today=today)

        # Insertion index = count of existing items whose live score ranks at or above the
        # newcomer. Existing items keep their relative order; only the newcomer is placed by
        # score (manual reorder later overrides everything). A non-album row in a mixed bucket
        # has no album to score → treated as 0.0 so scored albums float above it.
        insert_idx = 0
        for it in existing:
            it_score = (
                self._score(it.album, today=today)[0]
                if (getattr(it, "item_type", "album") == "album" and it.album is not None)
                else 0.0
            )
            if it_score >= new_score:
                insert_idx += 1
            else:
                break

        self._check_item_rate_limit(db, user_id, daily_cap)

        item = ReviewBucketItem(
            bucket_id=bucket.id,
            album_id=album_id,
            item_type="album",
            note=note,
            rec_reason=rec_reason,
            position=insert_idx,
        )
        # Renumber the whole column 0..n from the final ordering so positions stay dense and
        # idempotent (the newcomer shifts later items down by one).
        final_order = existing[:insert_idx] + [item] + existing[insert_idx:]
        for pos, it in enumerate(final_order):
            it.position = pos

        db.add(item)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise DuplicateItemError(album_id)
        db.refresh(item)
        return item

    def _add_typed_item(
        self,
        db: Session,
        bucket: ReviewBucket,
        *,
        user_id: uuid.UUID,
        item_type: str,
        track_id: Optional[str],
        review_target_id: Optional[str],
        note: Optional[str],
        snapshot: Optional[Any],
        artist_id: Optional[str] = None,
        daily_cap: Optional[int] = None,
    ) -> ReviewBucketItem:
        """Non-album kinds (track/review/playback/snapshot/artist). Appended at the end (no
        album score); per-kind dedup matches the V30/V32 partial-uniques; snapshot additionally
        writes one append-only bucket_item_snapshots side-row."""
        existing = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket.id)
            .order_by(ReviewBucketItem.position)
            .all()
        )
        next_pos = max((it.position for it in existing), default=-1) + 1
        kwargs: dict = dict(
            bucket_id=bucket.id, item_type=item_type, note=note, position=next_pos
        )

        if item_type in ("track", "playback"):
            if not track_id:
                raise ValueError("track_id required")
            # ARCH-entity-interaction-v2 Step 5 — a source that only knows the Spotify
            # track id (e.g. the liked-tracks mirror, which has no internal Track row
            # reference) sends that instead of our UUID PK. Resolve either: try the PK
            # first (the common case — every existing caller already had our id), fall
            # back to spotify_id. A raw non-UUID string reaching `Track.id ==` used to
            # be an unhandled DB type error (500) rather than the intended 404.
            try:
                track_uuid: Optional[uuid.UUID] = uuid.UUID(track_id)
            except ValueError:
                track_uuid = None
            track = (
                db.query(Track).filter(Track.id == track_uuid).first()
                if track_uuid is not None
                else None
            )
            if track is None:
                track = db.query(Track).filter(Track.spotify_id == track_id).first()
            if track is None:
                raise TrackNotFoundError(track_id)
            resolved_track_id = track.id
            kwargs["track_id"] = resolved_track_id
            # 'track' collections de-dupe (uq_..._track); 'playback' (queue) allows dups (D8).
            if item_type == "track" and any(
                it.item_type == "track" and str(it.track_id) == str(resolved_track_id)
                for it in existing
            ):
                raise DuplicateItemError(resolved_track_id)
        elif item_type == "review":
            if not review_target_id:
                raise ValueError("review_target_id required")
            if db.query(Post).filter(Post.id == review_target_id).first() is None:
                raise ReviewTargetNotFoundError(review_target_id)
            kwargs["review_target_id"] = review_target_id
            if any(
                it.item_type == "review"
                and str(it.review_target_id) == str(review_target_id)
                for it in existing
            ):
                raise DuplicateItemError(review_target_id)
        elif item_type == "artist":
            # FEAT-my-buckit-artist (V32): a single direct artist add. Validate the artist
            # exists, then per-kind dedup (uq_..._artist) → skip a re-add as a 409, mirroring
            # track/review. The source-expansion variant lives in expand_artist_source.
            if not artist_id:
                raise ValueError("artist_id required")
            if db.query(Artist).filter(Artist.id == artist_id).first() is None:
                raise ArtistNotFoundError(artist_id)
            kwargs["artist_id"] = artist_id
            if any(
                it.item_type == "artist" and str(it.artist_id) == str(artist_id)
                for it in existing
            ):
                raise DuplicateItemError(artist_id)
        elif item_type == "snapshot":
            if snapshot is None:
                raise ValueError("snapshot capture required")
            # snapshot allows dups; no typed FK on the membership row — the frozen data lives
            # in the bucket_item_snapshots side-row added after the flush below.
        else:
            raise ValueError(f"unsupported item_type: {item_type}")

        self._check_item_rate_limit(db, user_id, daily_cap)
        item = ReviewBucketItem(**kwargs)
        db.add(item)
        try:
            db.flush()  # assign item.id (needed for the snapshot FK) before commit
            if item_type == "snapshot":
                db.add(self._build_snapshot(item.id, snapshot))
            db.commit()
        except IntegrityError:
            db.rollback()
            # Only track/review/artist have a unique index (D8: playback/snapshot allow dups),
            # so an IntegrityError there is a concurrent-insert dup → 409. For playback/snapshot
            # it is some OTHER constraint error — don't mislabel it as a duplicate; let it
            # surface.
            if item_type in ("track", "review", "artist"):
                raise DuplicateItemError(track_id or review_target_id or artist_id)
            raise
        db.refresh(item)
        return item

    def expand_artist_source(
        self,
        db: Session,
        user_id: uuid.UUID,
        bucket_id: str,
        *,
        source_album_id: Optional[str] = None,
        source_track_id: Optional[str] = None,
        daily_cap: Optional[int] = None,
    ) -> Tuple[List[Artist], List[Artist]]:
        """FEAT-my-buckit-artist (V32): expand a featuring track / compilation album into its
        credited artists, adding each NOT-already-present one as an artist member. The SOURCE
        row is never stored — only its credited artists. Returns ``(added, skipped)`` Artist
        lists: ``added`` = newly inserted this call, ``skipped`` = credited artists already in
        the bucket (dedup). The Various Artists placeholder is excluded, so a VA compilation
        contributes zero artists (never a junk member). A mid-batch concurrent dup is treated
        as skipped (idempotent), never a 409 — each insert runs in its own SAVEPOINT so a race
        can't poison the whole batch.

        Every produced row is an artist row, so the artist-only type gate is satisfied on both
        General and Artist buckets (expansion is valid on either)."""
        bucket = self.get_bucket(db, bucket_id, user_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)
        # Sibling gap to add_item's (BUG-20 follow-up): this expansion path had no kind check
        # either, so a direct call could seed artist rows into the spotify_library mirror.
        self._assert_manual_add_allowed(bucket)
        # Exactly one source. The request validator guarantees this; guard direct callers.
        if bool(source_album_id) == bool(source_track_id):
            raise ValueError("exactly one of source_album_id / source_track_id required")

        # Resolve the source's structured credited artists (album_artists / track_artists).
        if source_album_id:
            source = (
                db.query(Album)
                .options(selectinload(Album.artists))
                .filter(Album.id == source_album_id)
                .first()
            )
            if source is None:
                raise AlbumNotFoundError(source_album_id)
        else:
            source = (
                db.query(Track)
                .options(selectinload(Track.artists))
                .filter(Track.id == source_track_id)
                .first()
            )
            if source is None:
                raise TrackNotFoundError(source_track_id)

        credited = self._credited_artists(source)

        existing = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket.id)
            .order_by(ReviewBucketItem.position)
            .all()
        )
        present_artist_ids = {
            str(it.artist_id)
            for it in existing
            if it.item_type == "artist" and it.artist_id is not None
        }
        next_pos = max((it.position for it in existing), default=-1) + 1

        rows_to_create = sum(
            1 for a in credited if str(a.id) not in present_artist_ids
        )
        self._check_item_rate_limit(
            db, user_id, daily_cap, rows_to_create=rows_to_create
        )

        added: List[Artist] = []
        skipped: List[Artist] = []
        for a in credited:
            if str(a.id) in present_artist_ids:
                skipped.append(a)
                continue
            try:
                with db.begin_nested():  # SAVEPOINT — a concurrent dup rolls back only this row
                    db.add(
                        ReviewBucketItem(
                            bucket_id=bucket.id,
                            item_type="artist",
                            artist_id=a.id,
                            position=next_pos,
                        )
                    )
                    db.flush()
            except IntegrityError:
                # The same artist landed concurrently (uq_..._artist) → idempotent skip, not 409.
                skipped.append(a)
                continue
            added.append(a)
            present_artist_ids.add(str(a.id))
            next_pos += 1

        db.commit()
        return added, skipped

    def expand_album_tracks(
        self,
        db: Session,
        user_id: uuid.UUID,
        bucket_id: str,
        *,
        source_album_id: str,
        daily_cap: Optional[int] = None,
    ) -> List[Track]:
        """FEAT-playback-bucket-player Step 3: expand an album into its tracks, appended to the
        bucket as ``item_type='playback'`` rows **in album order**. Returns the tracks added, in
        the order they were appended. The SOURCE album row is never stored — dropping an album
        on the queue enqueues its tracks, not the album.

        Sibling of :meth:`expand_artist_source`, not a replacement: same source-expansion idiom
        (one POST /items call, the source is a `source_*` id, the response is an expansion
        summary), different produced kind. Unlike the artist expansion there is NO dedup and so
        no `skipped` list — the queue deliberately allows duplicates (FEAT-pocket-buckit D8: no
        partial unique on `item_type='playback'`), because re-queueing a track you already
        queued is a legitimate act, not a mistake to swallow.

        **Ordering.** ``tracks.track_no ASC NULLS LAST``, then ``created_at``/``id`` as a stable
        tiebreak so the append order is deterministic rather than whatever the planner returns.
        `track_no` is Spotify's `track_number`, written by the worker sync and by the music
        service's track repo, and `track_no ASC NULLS LAST` is already the canonical album-track
        order elsewhere in the system (myblog_music `track_repo.py`) — this reuses it rather
        than inventing a second one.

        **Known limit, stated rather than hidden**: the schema has NO disc-number column
        (`tracks` carries `track_no` only). Spotify's `track_number` restarts at 1 on each disc,
        so a multi-disc album interleaves its discs here. That is a pre-existing modelling gap
        shared by every album-track read in the product, not something this method introduces;
        fixing it means a `disc_no` column and a backfill, which is out of this step's scope.
        """
        bucket = self.get_bucket(db, bucket_id, user_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)
        # Produces playback rows, so it must clear the same gate the single-row path clears —
        # an album dropped on an Artist bucket is still rejected.
        self._assert_item_type_allowed(bucket, "playback")
        # Sibling gap to add_item's (BUG-20 follow-up): without this, a direct call could seed
        # track rows into the spotify_library mirror via the album→tracks expansion path.
        self._assert_manual_add_allowed(bucket)

        album = db.query(Album).filter(Album.id == source_album_id).first()
        if album is None:
            raise AlbumNotFoundError(source_album_id)

        tracks = (
            db.query(Track)
            # The route serializes each returned track through _track_brief, which reads
            # track.artists — eager-load it so a 20-track album is one extra SELECT, not 20.
            .options(selectinload(Track.artists))
            .filter(Track.album_id == album.id)
            .order_by(
                Track.track_no.asc().nullslast(),
                Track.created_at.asc(),
                Track.id.asc(),
            )
            .all()
        )
        if not tracks:
            # A catalog album whose tracks were never synced. Nothing to queue; the route
            # answers 200 (no-op) exactly as it does for a zero-artist VA compilation.
            return []

        self._check_item_rate_limit(db, user_id, daily_cap, rows_to_create=len(tracks))

        next_pos = db.execute(
            select(func.coalesce(func.max(ReviewBucketItem.position), -1)).where(
                ReviewBucketItem.bucket_id == bucket.id
            )
        ).scalar_one()
        next_pos = int(next_pos) + 1

        for track in tracks:
            db.add(
                ReviewBucketItem(
                    bucket_id=bucket.id,
                    item_type="playback",
                    track_id=track.id,
                    position=next_pos,
                )
            )
            next_pos += 1
        db.commit()
        return tracks

    @staticmethod
    def _credited_artists(source: Album | Track) -> List[Artist]:
        """Return a source's distinct catalog credits, excluding the VA sentinel.

        Shared by artist-bucket source expansion and the personal release-tracking
        import preview so both features interpret album/track credits identically.
        """
        seen_ids: set[str] = set()
        credited: List[Artist] = []
        for artist in source.artists:
            artist_id = str(artist.id)
            if artist.name == VARIOUS_ARTISTS or artist_id in seen_ids:
                continue
            seen_ids.add(artist_id)
            credited.append(artist)
        return credited

    def bucket_catalog_artists(
        self,
        db: Session,
        user_id: uuid.UUID,
        bucket_id: uuid.UUID,
    ) -> List[Artist]:
        """Distinct catalog artists represented by one member-owned bucket.

        Album and track/playback rows expand through their structured credits;
        artist rows contribute their direct artist. Source album/track drops are
        persisted by ``expand_artist_source`` as direct artist rows, so they flow
        through the same path. Ownership is part of the bucket lookup and a miss
        is deliberately indistinguishable from another member's bucket.
        """
        bucket = (
            db.query(ReviewBucket)
            .options(
                selectinload(ReviewBucket.items)
                .selectinload(ReviewBucketItem.album)
                .selectinload(Album.artists),
                selectinload(ReviewBucket.items)
                .selectinload(ReviewBucketItem.track)
                .selectinload(Track.artists),
                selectinload(ReviewBucket.items).selectinload(ReviewBucketItem.artist),
            )
            .filter(
                ReviewBucket.id == bucket_id,
                ReviewBucket.user_id == user_id,
            )
            .first()
        )
        if bucket is None:
            raise BucketNotFoundError(str(bucket_id))

        artists_by_id: dict[str, Artist] = {}
        for item in bucket.items:
            if item.item_type == "artist" and item.artist is not None:
                artists_by_id.setdefault(str(item.artist.id), item.artist)
            elif item.item_type == "album" and item.album is not None:
                for artist in self._credited_artists(item.album):
                    artists_by_id.setdefault(str(artist.id), artist)
            elif item.item_type in ("track", "playback") and item.track is not None:
                for artist in self._credited_artists(item.track):
                    artists_by_id.setdefault(str(artist.id), artist)

        return sorted(
            artists_by_id.values(),
            key=lambda artist: (artist.name.casefold(), str(artist.id)),
        )

    @staticmethod
    def _build_snapshot(item_id, snap: Any) -> BucketItemSnapshot:
        """Build an append-only bucket_item_snapshots row from a SnapshotCaptureRequest-shaped
        object. captured_at + schema_version are server defaults; a refresh is a NEW row (never
        an UPDATE), so capture is structurally never-silently-overwriting."""
        return BucketItemSnapshot(
            item_id=item_id,
            kind=snap.kind,
            as_of=snap.as_of,
            metric=snap.metric,
            range_from=snap.range_from,
            range_to=snap.range_to,
            unit=snap.unit,
            total=snap.total,
            unresolved=snap.unresolved,
            unclassified=snap.unclassified,
            frozen=snap.frozen,
            source_album_ids=[uuid.UUID(str(x)) for x in (snap.source_album_ids or [])],
        )

    def update_item(
        self,
        db: Session,
        user_id: uuid.UUID,
        bucket_id: str,
        item_id: str,
        *,
        note: Optional[str] = None,
        status: Optional[str] = None,
        post_id: Optional[str] = None,
        research_selected: Optional[bool] = None,
        prep_tonight: Optional[bool] = None,
    ) -> ReviewBucketItem:
        # Ownership: a member can only touch items in their own bucket. A miss →
        # 404 (never reveal another member's item exists).
        if self.get_bucket(db, bucket_id, user_id) is None:
            raise ItemNotFoundError(item_id)
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
        if research_selected is not None:
            # FEAT-album-research-notes: per-item auto-research checkbox.
            item.research_selected = bool(research_selected)
        if prep_tonight is not None:
            # FEAT-editor-buckit Stage 1: "오늘 밤 키우기" gate for the nightly job.
            item.prep_tonight = bool(prep_tonight)
        db.commit()
        db.refresh(item)
        return item

    def grow_nightly(
        self,
        db: Session,
        owner_sub: str,
        album_id: uuid.UUID,
        post_id: uuid.UUID,
    ) -> int:
        """FIX-nightly-draft-identity: server-side grow-once for the 03:00 draft agent.

        Stamps `post_id` and clears `prep_tonight` on every OWNER-owned checked memo
        for `album_id`. The acting user is pinned to OWNER_SUB by the route — never
        taken from the request body — so the agent can act *for* the owner without
        being able to act *as* an arbitrary user (the impersonation primitive Phase A
        refuses; Phase B swaps this pin for bucket-derived ownership, nothing else).

        The generic item PATCH cannot serve this: it resolves the acting user from
        the verified JWT sub, and the agent owns no buckets, so it 404s by design.

        Guarantees: the post must exist and be a draft; items already carrying a
        post_id are never overwritten; idempotent — a second call matches zero rows
        and returns 0. Item ids are sorted before the UPDATE (row-lock-order
        convention). Single transaction, committed here.
        """
        if not owner_sub:
            # local/dev has no configured owner; nothing can match — never guess.
            return 0
        post = db.get(Post, post_id)
        if post is None:
            raise GrowPostNotFoundError(str(post_id))
        if post.status != "draft":
            raise GrowPostNotDraftError(str(post_id))

        item_ids = sorted(
            row[0]
            for row in db.query(ReviewBucketItem.id)
            .join(ReviewBucket, ReviewBucketItem.bucket_id == ReviewBucket.id)
            .filter(
                ReviewBucket.user_id == uuid.UUID(owner_sub),
                ReviewBucketItem.album_id == album_id,
                ReviewBucketItem.prep_tonight.is_(True),
                ReviewBucketItem.post_id.is_(None),
            )
            .all()
        )
        if not item_ids:
            return 0
        db.query(ReviewBucketItem).filter(ReviewBucketItem.id.in_(item_ids)).update(
            {"post_id": post_id, "prep_tonight": False}, synchronize_session=False
        )
        db.commit()
        return len(item_ids)

    def delete_item(
        self, db: Session, user_id: uuid.UUID, bucket_id: str, item_id: str
    ) -> bool:
        if self.get_bucket(db, bucket_id, user_id) is None:
            return False
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

    def reorder(self, db: Session, user_id: uuid.UUID, buckets: List[dict]) -> None:
        """Idempotently re-apply a drag result.

        Payload: ``[{id, item_ids:[...]}, ...]`` — for each listed bucket, the
        item_ids in their new top→bottom order. An item appearing under a bucket
        other than its current one is a cross-bucket move (bucket_id reassigned).
        Positions are rewritten 0..n so repeated calls converge.
        """
        # Validate target buckets up front, capturing each one's type for the gate below.
        bucket_ids = [b["id"] for b in buckets]
        bucket_rows = (
            db.query(ReviewBucket.id, ReviewBucket.type, ReviewBucket.kind)
            .filter(ReviewBucket.id.in_(bucket_ids))
            .filter(ReviewBucket.user_id == user_id)
            .all()
        )
        bucket_type_by_id = {str(bid): btype for bid, btype, _ in bucket_rows}
        bucket_kind_by_id = {str(bid): bkind for bid, _, bkind in bucket_rows}
        missing = [bid for bid in bucket_ids if str(bid) not in bucket_type_by_id]
        if missing:
            raise BucketNotFoundError(missing[0])

        # Collect every referenced item id and load in one query.
        all_item_ids = [iid for b in buckets for iid in b.get("item_ids", [])]
        # Scope items to the caller's buckets (join) so a reorder can't drag another
        # member's item into one of my buckets by referencing its id.
        items_by_id = {
            str(it.id): it
            for it in db.query(ReviewBucketItem)
            .join(ReviewBucket, ReviewBucketItem.bucket_id == ReviewBucket.id)
            .filter(ReviewBucketItem.id.in_(all_item_ids))
            .filter(ReviewBucket.user_id == user_id)
            .all()
        }
        unknown = [iid for iid in all_item_ids if str(iid) not in items_by_id]
        if unknown:
            raise ItemNotFoundError(unknown[0])

        # FEAT-my-buckit-artist (V32): the artist-only type gate also guards this move path —
        # not just add_item — so a cross-bucket drag can't park a non-artist item in an Artist
        # bucket (the hard invariant has no cross-table DB CHECK to fall back on). Validate the
        # whole batch BEFORE mutating so a bad move rejects atomically (no partial reorder).
        for b in buckets:
            if bucket_type_by_id.get(str(b["id"])) != "artist":
                continue
            for item_id in b.get("item_ids", []):
                item = items_by_id[str(item_id)]
                if item.item_type != "artist":
                    raise BucketTypeError(
                        "this bucket only holds artists; non-artist items are rejected"
                    )

        # Sibling gap to add_item's (BUG-20 follow-up): a drag-driven cross-bucket move is a
        # second "add" path this guard must also cover — a reorder call can relocate a foreign
        # item into spotify_library just as easily as add_item could. Items already resident
        # there (being reordered in place, not moved in) are unaffected.
        for b in buckets:
            if bucket_kind_by_id.get(str(b["id"])) != SPOTIFY_LIBRARY_BUCKET_KIND:
                continue
            for item_id in b.get("item_ids", []):
                item = items_by_id[str(item_id)]
                if str(item.bucket_id) != str(b["id"]):
                    raise SystemBucketError(
                        "spotify_library is sync-owned and cannot receive a manual add"
                    )

        for b in buckets:
            for pos, item_id in enumerate(b.get("item_ids", [])):
                item = items_by_id[str(item_id)]
                item.bucket_id = b["id"]
                item.position = pos

        db.commit()

    # ── the Playback Bucket (FEAT-playback-bucket-player) ─────────────────────────

    def get_or_create_playback_bucket(
        self, db: Session, user_id: uuid.UUID
    ) -> ReviewBucket:
        """Return the member's Playback Bucket, creating it (as a root column appended after
        their existing buckets) if it does not exist yet. Idempotent; safe to call on every
        bucket-tree read, which is exactly how it gets created — lazily, on first read, rather
        than by a migration backfill (Step 2 rationale).

        Verbatim the :meth:`get_or_create_spotify_library_bucket` idiom, including the
        IntegrityError race arm: two concurrent reads can both pass the ``.first()`` guard, and
        ``idx_review_buckets_single_playback`` (UNIQUE (user_id) WHERE kind='playback_queue',
        V51) rejects the loser, which then returns the winner's row.

        Eligibility gates PLAYING, never EXISTING (T1) — so no Spotify capability check happens
        here. A member who loses Spotify permission keeps the bucket and every queued row.
        """
        bucket = (
            db.query(ReviewBucket)
            .filter(
                ReviewBucket.kind == PLAYBACK_BUCKET_KIND,
                ReviewBucket.user_id == user_id,
            )
            .first()
        )
        if bucket is not None:
            return bucket
        next_pos = db.execute(
            select(func.coalesce(func.max(ReviewBucket.position), -1)).where(
                ReviewBucket.user_id == user_id
            )
        ).scalar_one()
        bucket = ReviewBucket(
            user_id=user_id,
            name=PLAYBACK_BUCKET_NAME,
            kind=PLAYBACK_BUCKET_KIND,
            type=PLAYBACK_BUCKET_TYPE,
            position=int(next_pos) + 1,
        )
        db.add(bucket)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return (
                db.query(ReviewBucket)
                .filter(
                    ReviewBucket.kind == PLAYBACK_BUCKET_KIND,
                    ReviewBucket.user_id == user_id,
                )
                .one()
            )
        db.refresh(bucket)
        return bucket

    # ── Spotify Library sync (FEAT-spotify-library-sync) ──────────────────────────
    # The single kind='spotify_library' bucket mirrors the owner's Spotify saved
    # albums. Get-or-create is the BACKEND's job (the worker creates none). The POST
    # endpoint only enqueues an async job — Spotify is never touched here (rule #9).

    def get_or_create_spotify_library_bucket(
        self, db: Session, owner_id: uuid.UUID
    ) -> ReviewBucket:
        """Return the owner's kind='spotify_library' bucket, creating it (as a root
        column appended after existing buckets) if it does not exist yet. The DB
        partial-unique index (V40: per-user) guarantees at most one per user; the
        Spotify lane is owner-only until Phase 3b, so owner_id is always the owner."""
        bucket = (
            db.query(ReviewBucket)
            .filter(
                ReviewBucket.kind == SPOTIFY_LIBRARY_BUCKET_KIND,
                ReviewBucket.user_id == owner_id,
            )
            .first()
        )
        if bucket is not None:
            return bucket
        next_pos = db.execute(
            select(func.coalesce(func.max(ReviewBucket.position), -1)).where(
                ReviewBucket.user_id == owner_id
            )
        ).scalar_one()
        bucket = ReviewBucket(
            user_id=owner_id,
            name=SPOTIFY_LIBRARY_BUCKET_NAME,
            kind=SPOTIFY_LIBRARY_BUCKET_KIND,
            position=int(next_pos) + 1,
        )
        db.add(bucket)
        try:
            db.commit()
        except IntegrityError:
            # Two concurrent POSTs both passed the .first() guard and raced to
            # insert; the partial-unique index idx_review_buckets_single_spotify_library
            # (per-user) rejects the loser. Roll back and return the winner's row.
            db.rollback()
            return (
                db.query(ReviewBucket)
                .filter(
                    ReviewBucket.kind == SPOTIFY_LIBRARY_BUCKET_KIND,
                    ReviewBucket.user_id == owner_id,
                )
                .one()
            )
        db.refresh(bucket)
        return bucket

    def library_last_synced_at(self, db: Session) -> Optional[datetime]:
        """max(spotify_library_albums.last_synced_at), or None when nothing synced.
        Drives both the debounce window and the GET-state poll timestamp."""
        return db.execute(
            select(func.max(SpotifyLibraryAlbum.last_synced_at))
        ).scalar_one()

    def library_sync_debounced(
        self,
        db: Session,
        *,
        window_seconds: int = LIBRARY_SYNC_DEBOUNCE_SECONDS,
        now: Optional[datetime] = None,
    ) -> bool:
        """True when a sync ran within ``window_seconds`` (so a fresh POST is a
        no-op). Derived from max(last_synced_at); None (never synced) => not
        debounced. Both sides are coerced to tz-aware UTC for a safe comparison."""
        last = self.library_last_synced_at(db)
        if last is None:
            return False
        now = now or datetime.now(timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - last) < timedelta(seconds=window_seconds)

    def list_spotify_library_albums(
        self, db: Session
    ) -> List[SpotifyLibraryAlbum]:
        """Every spotify_library_albums row (with its joined Album eager-loaded),
        ordered newest-touched first. Read-only — populated by the worker."""
        return (
            db.query(SpotifyLibraryAlbum)
            .order_by(
                SpotifyLibraryAlbum.last_synced_at.desc().nullslast(),
                SpotifyLibraryAlbum.created_at.desc(),
            )
            .all()
        )

    def get_spotify_library_state(
        self, db: Session
    ) -> Tuple[Optional[ReviewBucket], Optional[datetime], List[SpotifyLibraryAlbum]]:
        """Read the special bucket (if any), the last-synced timestamp, and the
        spotify_library_albums rows for the GET /state endpoint. Pure read — the
        bucket is NOT created here (only the POST sync get-or-creates it)."""
        bucket = (
            db.query(ReviewBucket)
            .filter(ReviewBucket.kind == SPOTIFY_LIBRARY_BUCKET_KIND)
            .first()
        )
        last_synced_at = self.library_last_synced_at(db)
        albums = self.list_spotify_library_albums(db)
        return bucket, last_synced_at, albums
