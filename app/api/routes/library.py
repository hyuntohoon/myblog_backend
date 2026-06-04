# app/api/routes/library.py
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.schemas import (
    AddToListenRequest,
    AlbumBrief,
    ReviewedAlbumResponse,
    ReviewedResponse,
    ToListenItemResponse,
    ToListenReorderRequest,
    ToListenResponse,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_library_service
from app.services.library_service import (
    AlbumNotFoundError,
    DuplicateItemError,
    ItemNotFoundError,
    LibraryService,
)

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


def _to_listen_response(item) -> ToListenItemResponse:
    return ToListenItemResponse(
        id=str(item.id),
        album_id=str(item.album_id),
        position=item.position,
        note=item.note,
        added_at=item.added_at,
        album=_album_brief(item.album),
    )


# ── reads (edge_guard only — no JWT; covered by GET /api/{proxy+}) ──────────────

@router.get("/to-listen", response_model=ToListenResponse)
def list_to_listen(
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    items = svc.list_to_listen(db)
    return ToListenResponse(items=[_to_listen_response(it) for it in items])


@router.get("/reviewed", response_model=ReviewedResponse)
def list_reviewed(
    group_by: str = "album",
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
):
    # group_by is fixed to "album" for now (D20); accepted as a query param so the
    # contract is forward-compatible if a by-review grouping is ever added.
    rows = svc.list_reviewed(db)
    return ReviewedResponse(
        items=[
            ReviewedAlbumResponse(
                album_id=str(album.id),
                review_ids=review_ids,
                album=_album_brief(album),
            )
            for album, review_ids in rows
        ]
    )


# ── to-listen mutations (Cognito JWT) ───────────────────────────────────────────
# /to-listen/reorder is declared before /to-listen/{item_id} so the literal path
# is unambiguous.

@router.put("/to-listen/reorder", status_code=204)
def reorder_to_listen(
    req: ToListenReorderRequest,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        svc.reorder_to_listen(db, req.item_ids)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)


@router.post(
    "/to-listen",
    response_model=ToListenItemResponse,
    status_code=201,
    responses={409: {"description": "Album already in the to-listen queue"}},
)
def add_to_listen(
    req: AddToListenRequest,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        item = svc.add_to_listen(db, album_id=req.album_id, note=req.note)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except DuplicateItemError:
        raise HTTPException(
            status_code=409, detail="Album already in the to-listen queue"
        )
    return _to_listen_response(item)


@router.delete("/to-listen/{item_id}", status_code=204)
def delete_to_listen(
    item_id: str,
    db: Session = Depends(get_db),
    svc: LibraryService = Depends(get_library_service),
    _claims: Dict = Depends(require_cognito_token),
):
    if not svc.delete_to_listen(db, item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)
