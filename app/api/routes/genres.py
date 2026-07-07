# app/api/routes/genres.py
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import (
    CreateGenreRequest,
    GenreEdge,
    GenreNode,
    GenreTreeResponse,
    UpdateGenreRequest,
)
from app.core.auth import require_owner
from app.db.session import get_db
from app.di import get_genre_service
from app.services.genre_service import (
    DuplicateSlugError,
    GenreNotFoundError,
    GenreService,
    ParentNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _node(g) -> GenreNode:
    """Serialize a Genre ORM row (with transient `children_nodes`) into the nested
    response shape. Recurses children depth-first (2-tier today, view-agnostic)."""
    return GenreNode(
        id=str(g.id),
        slug=g.slug,
        label=g.label,
        parent_id=str(g.parent_id) if g.parent_id else None,
        definition_md=g.definition_md,
        position=g.position,
        album_count=int(getattr(g, "album_count", 0) or 0),
        children=[_node(c) for c in (getattr(g, "children_nodes", []) or [])],
    )


# ── read (edge_guard only — no JWT; covered by GET /api/{proxy+}) ───────────────

@router.get("/tree", response_model=GenreTreeResponse)
def genre_tree(
    db: Session = Depends(get_db),
    svc: GenreService = Depends(get_genre_service),
):
    roots = svc.list_tree(db)
    edges = [GenreEdge(from_id=f, to_id=t, type=ty) for f, t, ty in svc.list_edges(db)]
    return GenreTreeResponse(genres=[_node(r) for r in roots], edges=edges)


# ── mutations (Cognito JWT — copy buckets_post, NOT categories_post) ────────────

@router.post("", response_model=GenreNode, status_code=201)
def create_genre(
    req: CreateGenreRequest,
    db: Session = Depends(get_db),
    svc: GenreService = Depends(get_genre_service),
    _claims: Dict = Depends(require_owner),
):
    try:
        genre = svc.create(
            db,
            slug=req.slug,
            label=req.label,
            parent_id=req.parent_id,
            definition_md=req.definition_md,
            position=req.position,
        )
    except ParentNotFoundError:
        raise HTTPException(status_code=400, detail="Parent genre not found")
    except DuplicateSlugError:
        raise HTTPException(status_code=409, detail="Genre slug already exists")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _node(genre)


@router.put("/{genre_id}", response_model=GenreNode)
def update_genre(
    genre_id: str,
    req: UpdateGenreRequest,
    db: Session = Depends(get_db),
    svc: GenreService = Depends(get_genre_service),
    _claims: Dict = Depends(require_owner),
):
    updates = req.model_dump(exclude_unset=True)
    try:
        genre = svc.update(db, genre_id, **updates)
    except GenreNotFoundError:
        raise HTTPException(status_code=404, detail="Genre not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _node(genre)
