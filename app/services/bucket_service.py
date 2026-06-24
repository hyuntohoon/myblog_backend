# app/services/bucket_service.py
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from myblog_shared_db.models import (
    Album,
    BucketItemSnapshot,
    Post,
    ReviewBucket,
    ReviewBucketItem,
    SpotifyLibraryAlbum,
    Track,
    post_albums_table as post_albums,
)

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
        """Nested tree of buckets. Returns only ROOT buckets (parent_id IS NULL);
        each node carries its descendants on a transient ``children_nodes`` list
        (recursive), every sibling level ordered by (position, created_at). Items
        stay populated per bucket via the relationship's order_by.

        ``children_nodes`` is a non-column attribute attached for serialization —
        it does not exist on the ORM model and is never persisted.
        """
        all_buckets = (
            db.query(ReviewBucket)
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
    ) -> ReviewBucket:
        """Reparent ``bucket_id`` under ``parent_id`` (None => root) at ``position``.

        Cycle prevention: 400 (ValueError) if parent_id == bucket_id, or if
        bucket_id is an ancestor of parent_id (moving a node under its own
        descendant). 404 (BucketNotFoundError) if bucket or parent is missing.
        After reparenting, siblings sharing the new parent_id are renumbered so
        the moved bucket lands at ``position`` and positions are contiguous 0..n.
        """
        bucket = self.get_bucket(db, bucket_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)

        if parent_id is not None:
            parent = self.get_bucket(db, parent_id)
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
        color: Any = _UNSET,
        position: Optional[int] = None,
        is_done: Optional[bool] = None,
        research_mode: Optional[str] = None,
        is_public: Optional[bool] = None,
    ) -> ReviewBucket:
        bucket = self.get_bucket(db, bucket_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)
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
            # FEAT-public-bucket-multiuser Scope A: opt-in public visibility. The
            # spotify_library bucket must never be published — guard here so a direct
            # PATCH can't expose the owner's Spotify library through the public viewer.
            if bool(is_public) and bucket.kind == "spotify_library":
                raise ValueError("the Spotify library bucket cannot be made public")
            bucket.is_public = bool(is_public)
        db.commit()
        db.refresh(bucket)
        return bucket

    def list_public_buckets(self, db: Session) -> List[ReviewBucket]:
        """Flat, position-ordered list of buckets the owner has published
        (is_public=true) AND that are normal review columns (kind='review').

        Deliberately FLAT (no nesting): exposing the parent/child tree would leak
        the existence/structure of private buckets via a published child. Each
        published bucket is a standalone public 'shelf'. The route projects these
        through the whitelisted Public* schemas (no private item fields).
        """
        return (
            db.query(ReviewBucket)
            .filter(ReviewBucket.is_public.is_(True))
            .filter(ReviewBucket.kind == "review")
            .order_by(ReviewBucket.position, ReviewBucket.created_at)
            .all()
        )

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
        item_type: str = "album",
        album_id: Optional[str] = None,
        track_id: Optional[str] = None,
        review_target_id: Optional[str] = None,
        note: Optional[str] = None,
        snapshot: Optional[Any] = None,
        today: Optional[date] = None,
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

        Raises BucketNotFoundError / AlbumNotFoundError / TrackNotFoundError /
        ReviewTargetNotFoundError / DuplicateItemError, mapped to HTTP by the route."""
        bucket = self.get_bucket(db, bucket_id)
        if bucket is None:
            raise BucketNotFoundError(bucket_id)

        if item_type == "album":
            return self._add_album_item(db, bucket, album_id=album_id, note=note, today=today)
        return self._add_typed_item(
            db,
            bucket,
            item_type=item_type,
            track_id=track_id,
            review_target_id=review_target_id,
            note=note,
            snapshot=snapshot,
        )

    def _add_album_item(
        self,
        db: Session,
        bucket: ReviewBucket,
        *,
        album_id: Optional[str],
        note: Optional[str],
        today: Optional[date],
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
        item_type: str,
        track_id: Optional[str],
        review_target_id: Optional[str],
        note: Optional[str],
        snapshot: Optional[Any],
    ) -> ReviewBucketItem:
        """Non-album kinds (track/review/playback/snapshot). Appended at the end (no album
        score); per-kind dedup matches the V30 partial-uniques; snapshot additionally writes
        one append-only bucket_item_snapshots side-row."""
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
            if db.query(Track).filter(Track.id == track_id).first() is None:
                raise TrackNotFoundError(track_id)
            kwargs["track_id"] = track_id
            # 'track' collections de-dupe (uq_..._track); 'playback' (queue) allows dups (D8).
            if item_type == "track" and any(
                it.item_type == "track" and str(it.track_id) == str(track_id)
                for it in existing
            ):
                raise DuplicateItemError(track_id)
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
        elif item_type == "snapshot":
            if snapshot is None:
                raise ValueError("snapshot capture required")
            # snapshot allows dups; no typed FK on the membership row — the frozen data lives
            # in the bucket_item_snapshots side-row added after the flush below.
        else:
            raise ValueError(f"unsupported item_type: {item_type}")

        item = ReviewBucketItem(**kwargs)
        db.add(item)
        try:
            db.flush()  # assign item.id (needed for the snapshot FK) before commit
            if item_type == "snapshot":
                db.add(self._build_snapshot(item.id, snapshot))
            db.commit()
        except IntegrityError:
            db.rollback()
            # Only track/review have a unique index (D8: playback/snapshot allow dups), so an
            # IntegrityError there is a concurrent-insert dup → 409. For playback/snapshot it is
            # some OTHER constraint error — don't mislabel it as a duplicate; let it surface.
            if item_type in ("track", "review"):
                raise DuplicateItemError(track_id or review_target_id)
            raise
        db.refresh(item)
        return item

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
        bucket_id: str,
        item_id: str,
        *,
        note: Optional[str] = None,
        status: Optional[str] = None,
        post_id: Optional[str] = None,
        research_selected: Optional[bool] = None,
        prep_tonight: Optional[bool] = None,
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
        if research_selected is not None:
            # FEAT-album-research-notes: per-item auto-research checkbox.
            item.research_selected = bool(research_selected)
        if prep_tonight is not None:
            # FEAT-editor-buckit Stage 1: "오늘 밤 키우기" gate for the nightly job.
            item.prep_tonight = bool(prep_tonight)
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

    # ── Spotify Library sync (FEAT-spotify-library-sync) ──────────────────────────
    # The single kind='spotify_library' bucket mirrors the owner's Spotify saved
    # albums. Get-or-create is the BACKEND's job (the worker creates none). The POST
    # endpoint only enqueues an async job — Spotify is never touched here (rule #9).

    def get_or_create_spotify_library_bucket(self, db: Session) -> ReviewBucket:
        """Return the single kind='spotify_library' bucket, creating it (as a root
        column appended after existing buckets) if it does not exist yet. The DB
        partial-unique index guarantees at most one such bucket."""
        bucket = (
            db.query(ReviewBucket)
            .filter(ReviewBucket.kind == SPOTIFY_LIBRARY_BUCKET_KIND)
            .first()
        )
        if bucket is not None:
            return bucket
        next_pos = db.execute(
            select(func.coalesce(func.max(ReviewBucket.position), -1))
        ).scalar_one()
        bucket = ReviewBucket(
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
            # rejects the loser. Roll back and return the winner's row.
            db.rollback()
            return (
                db.query(ReviewBucket)
                .filter(ReviewBucket.kind == SPOTIFY_LIBRARY_BUCKET_KIND)
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
