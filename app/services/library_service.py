# app/services/library_service.py
from __future__ import annotations

from typing import List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from myblog_shared_db.models import Album, LibraryItem


class AlbumNotFoundError(Exception):
    """Raised when an album id does not exist. Route maps to 404."""


class LibraryService:
    """Single-user personal library: one row per album with an exclusive status
    (listening / listened / reviewed / wishlist).

    Single user → no user_id / ownership checks. Transaction boundary lives in
    the service (commit once per mutation), mirroring BucketService. The
    UNIQUE(album_id) constraint guarantees at most one status per album, so
    set_status is an upsert keyed on album_id.
    """

    # ── reads ─────────────────────────────────────────────────────────────────

    def list_items(self, db: Session) -> List[LibraryItem]:
        """Every library row, most-recently-changed first."""
        return (
            db.query(LibraryItem)
            .order_by(LibraryItem.updated_at.desc(), LibraryItem.added_at.desc())
            .all()
        )

    # ── mutations ───────────────────────────────────────────────────────────────

    def set_status(
        self, db: Session, album_id: str, *, status: str
    ) -> Tuple[LibraryItem, bool]:
        """Upsert the library status for an album. Returns (item, created).

        Creating requires the album to exist (FK); a non-existent album raises
        AlbumNotFoundError (404) rather than surfacing an opaque IntegrityError.
        """
        album = db.query(Album).filter(Album.id == album_id).first()
        if album is None:
            raise AlbumNotFoundError(album_id)

        item = (
            db.query(LibraryItem).filter(LibraryItem.album_id == album_id).first()
        )
        created = item is None
        if item is None:
            item = LibraryItem(album_id=album_id, status=status)
            db.add(item)
        else:
            item.status = status
            # No onupdate on the column (matches ReviewBucketItem); bump
            # explicitly so list ordering reflects the latest change.
            item.updated_at = func.now()
        db.commit()
        db.refresh(item)
        return item, created

    def delete_item(self, db: Session, album_id: str) -> bool:
        """Remove an album from the library. Returns False if it wasn't there."""
        item = (
            db.query(LibraryItem).filter(LibraryItem.album_id == album_id).first()
        )
        if item is None:
            return False
        db.delete(item)
        db.commit()
        return True
