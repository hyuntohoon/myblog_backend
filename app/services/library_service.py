# app/services/library_service.py
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from myblog_shared_db.models import (
    Album,
    AlbumToListenItem,
    Post,
    SpotifyNowPlaying,
    SpotifyRecentAlbum,
    post_albums_table as post_albums,
)


class AlbumNotFoundError(Exception):
    """Raised when an album id does not exist. Route maps to 404."""


class ItemNotFoundError(Exception):
    """Raised when a to-listen item id does not exist. Route maps to 404."""


class DuplicateItemError(Exception):
    """Raised when an album is already in the to-listen queue. Route maps to 409."""


class LibraryService:
    """Member-dashboard Library tab (FEAT-member-dashboard Step 2, D18).

    Two of the three Library sources live here:
      - 들을 것 (to-listen): a manual, position-ordered queue (album_to_listen_items).
      - 평론한 앨범 (reviewed): a read-only view derived from published posts
        (post_albums ⋈ posts where status='published'), grouped by album.
    "최근 들은 앨범" (Spotify cache) is Step 3, not here.

    Single user → no user_id / ownership checks. Commit per mutation (mirrors
    BucketService).
    """

    # ── to-listen: reads ────────────────────────────────────────────────────────

    def list_to_listen(self, db: Session) -> List[AlbumToListenItem]:
        return (
            db.query(AlbumToListenItem)
            .order_by(AlbumToListenItem.position, AlbumToListenItem.added_at)
            .all()
        )

    # ── to-listen: mutations ──────────────────────────────────────────────────────

    def add_to_listen(
        self, db: Session, *, album_id: str, note: Optional[str] = None
    ) -> AlbumToListenItem:
        """Append an album to the end of the queue. Album must exist; an album
        already queued raises DuplicateItemError (UNIQUE album_id)."""
        album = db.query(Album).filter(Album.id == album_id).first()
        if album is None:
            raise AlbumNotFoundError(album_id)

        exists = (
            db.query(AlbumToListenItem.id)
            .filter(AlbumToListenItem.album_id == album_id)
            .first()
        )
        if exists is not None:
            raise DuplicateItemError(album_id)

        next_pos = db.execute(
            select(func.coalesce(func.max(AlbumToListenItem.position), -1))
        ).scalar_one()
        item = AlbumToListenItem(
            album_id=album_id, note=note, position=int(next_pos) + 1
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return item

    def delete_to_listen(self, db: Session, item_id: str) -> bool:
        item = (
            db.query(AlbumToListenItem)
            .filter(AlbumToListenItem.id == item_id)
            .first()
        )
        if item is None:
            return False
        db.delete(item)
        db.commit()
        return True

    def reorder_to_listen(self, db: Session, item_ids: List[str]) -> None:
        """Rewrite queue positions 0..n from the given top→bottom order. Same
        idempotent mechanism as bucket reorder; an unknown id raises
        ItemNotFoundError."""
        items_by_id = {
            str(it.id): it
            for it in db.query(AlbumToListenItem)
            .filter(AlbumToListenItem.id.in_(item_ids))
            .all()
        }
        unknown = [iid for iid in item_ids if str(iid) not in items_by_id]
        if unknown:
            raise ItemNotFoundError(unknown[0])

        for pos, item_id in enumerate(item_ids):
            items_by_id[str(item_id)].position = pos
        db.commit()

    # ── reviewed: derived view ────────────────────────────────────────────────────

    def list_reviewed(self, db: Session) -> List[Tuple[Album, List[str]]]:
        """One entry per album that has ≥1 published review, with the album's
        published post ids. Albums ordered by most-recent review first.

        Derived from post_albums ⋈ posts(status='published') — no table. The
        album↔review M:N is preserved (review_ids is a list).
        """
        rows = db.execute(
            select(post_albums.c.album_id, Post.id, Post.posted_date)
            .join(Post, Post.id == post_albums.c.post_id)
            .where(Post.status == "published")
            .order_by(Post.posted_date.desc())
        ).all()

        review_ids: Dict[str, List[str]] = {}
        latest: Dict[str, date] = {}
        for album_id, post_id, posted_date in rows:
            aid = str(album_id)
            review_ids.setdefault(aid, []).append(str(post_id))
            if aid not in latest:
                latest[aid] = posted_date  # first seen = newest (rows are desc)

        if not review_ids:
            return []

        albums = {
            str(a.id): a
            for a in db.query(Album).filter(Album.id.in_(list(review_ids))).all()
        }
        ordered = sorted(
            (aid for aid in review_ids if aid in albums),
            key=lambda aid: latest[aid],
            reverse=True,
        )
        return [(albums[aid], review_ids[aid]) for aid in ordered]

    # ── 최근 들은 앨범 + now-playing: read-only Spotify cache (Step 3, D25/D5) ────────
    # Populated by the worker (EventBridge cron + manual SQS refresh). These reads
    # never touch Spotify (hard rule #9).

    def list_recently_listened(self, db: Session) -> List[Tuple[Album, datetime]]:
        """The distinct recently-played album set, most-recently-played first.
        Returns (album, last_played_at) pairs."""
        rows = (
            db.query(SpotifyRecentAlbum)
            .order_by(SpotifyRecentAlbum.last_played_at.desc())
            .all()
        )
        return [(r.album, r.last_played_at) for r in rows if r.album is not None]

    def get_now_playing(self, db: Session) -> Optional[SpotifyNowPlaying]:
        """The single-row now-playing cache (id=1), or None if never synced."""
        return (
            db.query(SpotifyNowPlaying)
            .filter(SpotifyNowPlaying.id == 1)
            .first()
        )
