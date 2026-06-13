# app/services/genre_service.py
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from myblog_shared_db.models import AlbumGenre, Genre

# Sentinel for "argument not provided" — lets update() tell an omitted field apart
# from an explicit clear (mirrors bucket_service._UNSET). The route strips unsent
# fields via model_dump(exclude_unset=True).
_UNSET: Any = object()


class GenreNotFoundError(Exception):
    """Raised when a genre id does not exist. Route maps to 404."""


class ParentNotFoundError(Exception):
    """Raised when a create/update references a missing parent_id. Route maps to 400."""


class DuplicateSlugError(Exception):
    """Raised when a slug collides with the UNIQUE(slug) constraint. Route maps to 409."""


class GenreService:
    """Tier-0/tier-1 genre taxonomy (FEAT-genre-system).

    Single user → no ownership checks. Transaction boundary lives in the service
    (commit once per mutation), mirroring BucketService. The machine labeling
    layer (`album_genres`) is never touched here — this is the human-editable
    definition/tree surface only.
    """

    # ── reads ────────────────────────────────────────────────────────────────

    def list_tree(self, db: Session) -> List[Genre]:
        """Return tier-0 roots, each carrying its children on a transient
        `children_nodes` list (mirrors bucket_service's forest serialization).

        2-tier only by design (tier-1 RFC reuses parent_id) — one self-join in
        Python is cheaper and clearer than a recursive CTE for ≤~50 nodes.
        """
        rows = db.execute(
            select(Genre).order_by(Genre.position, Genre.label)
        ).scalars().all()
        # Album distribution for the /genres share-bars: one grouped count over
        # album_genres (all confidences), attached as the transient `album_count`.
        # Direct attachments per genre — tier-0 nodes are attached directly by the
        # machine pipeline, so no roll-up is needed at the current 2-tier depth.
        counts = dict(
            db.execute(
                select(AlbumGenre.genre_id, func.count()).group_by(AlbumGenre.genre_id)
            ).all()
        )
        by_parent: dict[Optional[Any], List[Genre]] = {}
        for g in rows:
            g.album_count = int(counts.get(g.id, 0))
            by_parent.setdefault(g.parent_id, []).append(g)
        for g in rows:
            g.children_nodes = by_parent.get(g.id, [])
        return by_parent.get(None, [])

    def get(self, db: Session, genre_id: str) -> Optional[Genre]:
        return db.query(Genre).filter(Genre.id == genre_id).first()

    # ── writes ───────────────────────────────────────────────────────────────

    def create(
        self,
        db: Session,
        *,
        slug: str,
        label: str,
        parent_id: Optional[str] = None,
        definition_md: str = "",
        position: Optional[int] = None,
    ) -> Genre:
        slug = (slug or "").strip()
        label = (label or "").strip()
        if not slug:
            raise ValueError("slug required")
        if not label:
            raise ValueError("label required")
        if parent_id is not None and self.get(db, parent_id) is None:
            raise ParentNotFoundError(parent_id)
        if position is None:
            # Append within the target tier (siblings share a parent_id).
            position = int(
                db.execute(
                    select(func.coalesce(func.max(Genre.position), -1)).where(
                        Genre.parent_id == parent_id
                    )
                ).scalar_one()
            ) + 1
        genre = Genre(
            slug=slug,
            label=label,
            parent_id=parent_id,
            definition_md=definition_md or "",
            position=int(position),
        )
        db.add(genre)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise DuplicateSlugError(slug)
        db.refresh(genre)
        return genre

    def update(
        self,
        db: Session,
        genre_id: str,
        *,
        label: Optional[str] = None,
        definition_md: Any = _UNSET,
        position: Optional[int] = None,
    ) -> Genre:
        genre = self.get(db, genre_id)
        if genre is None:
            raise GenreNotFoundError(genre_id)
        if label is not None:
            label = label.strip()
            if not label:
                raise ValueError("label cannot be empty")
            genre.label = label
        if definition_md is not _UNSET:
            # Explicit "" clears the definition; None is coerced to "" (NOT NULL).
            genre.definition_md = definition_md or ""
        if position is not None:
            genre.position = int(position)
        db.commit()
        db.refresh(genre)
        return genre
