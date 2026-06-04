# app/api/routes/library.py
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.schemas import (
    AlbumBrief,
    LibraryItemResponse,
    LibraryResponse,
    SetLibraryStatusRequest,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_library_service
from app.services.library_service import AlbumNotFoundError, LibraryService

logger = logging.getLogger(__name__)

router = APIRouter()


def _album_brief(album) -> AlbumBrief:
    return AlbumBrief(
        id=str(album.id),
        title=album.title,
        cover_url=album.cover_url,
        release_date=album.release_date,
        popularity=album.popularity,
        artist_names=[a.name for a in album.artists],
    )


def _item_response(item) -> LibraryItemResponse:
    return LibraryItemResponse(
        album_id=str(item.album_id),
        status=item.status,
        added_at=item.added_at,
        updated_at=item.updated_at,
        album=_album_brief(item.album),
    )


# ── reads (edge_guard only — no JWT; covered by GET /api/{proxy+}) ──────────────

@router.get("", response_model=LibraryResponse)
def list_library(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    items = svc.list_items(db)
    return LibraryResponse(items=[_item_response(it) for it in items])


# ── mutations (Cognito JWT) ─────────────────────────────────────────────────────

@router.put("/{album_id}", response_model=LibraryItemResponse)
def set_library_status(
    album_id: str,
    req: SetLibraryStatusRequest,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    _claims: Dict = Depends(require_cognito_token),
):
    """Upsert the library status for an album (set or change)."""
    try:
        item, _created = svc.set_status(db, album_id, status=req.status)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    return _item_response(item)


@router.delete("/{album_id}", status_code=204)
def delete_library_item(
    album_id: str,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    _claims: Dict = Depends(require_cognito_token),
):
    if not svc.delete_item(db, album_id):
        raise HTTPException(status_code=404, detail="Library item not found")
    return Response(status_code=204)
