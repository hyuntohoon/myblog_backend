# app/api/routes/research.py
# FEAT-album-research-notes Step 4 — writer-facing AI research notes (never public).
#
# Notes belong to the ALBUM, not the surface — the same two routes back the BucketBoard
# item and the /write editor. POST (manual trigger / restart / refine) is Cognito-JWT at
# the API Gateway (matching infra/apigateway.tf route). GET rides the edge_guard catch-all
# (api_get_proxy) like GET /api/buckets — no JWT route; reachable only via CloudFront.
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.schemas import (
    AlbumResearchResponse,
    ResearchStatusMapResponse,
    ResearchTriggerRequest,
)
from app.core.auth import require_owner
from app.core.ids import parse_uuid_list_or_400, parse_uuid_or_404
from app.db.session import get_db
from app.di import get_research_service
from app.services.research_service import (
    AlbumNotFoundError,
    ResearchService,
    ResearchStateError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _research_response(row) -> AlbumResearchResponse:
    return AlbumResearchResponse(
        album_id=str(row.album_id),
        prompt_version=row.prompt_version,
        status=row.status,
        model=row.model,
        result_md=row.result_md,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        search_count=row.search_count,
        error=row.error,
        refine_count=row.refine_count,
        last_instruction=row.last_instruction,
        requested_at=row.requested_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


@router.get("/status", response_model=ResearchStatusMapResponse)
def get_research_status_map(
    album_ids: str = Query(..., description="Comma-separated album ids."),
    db: Session = Depends(get_db),
    svc: ResearchService = Depends(get_research_service),
):
    """Batched {album_id -> status} for a board's covers in ONE query. Replaces
    the per-cover GET fan-out (album-count concurrent requests on mount) that hit
    the Lambda concurrency ceiling and 503'd. Unresearched albums are absent."""
    ids = [s for s in (album_ids.split(",") if album_ids else []) if s.strip()]
    # Cap to keep one board's worth; a huge list would be abusive, not a real board.
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="too many album_ids (max 500)")
    # A-3: a non-UUID entry reached the uuid column and 500'd the whole board.
    parsed = parse_uuid_list_or_400(ids, detail="malformed album_ids")
    return ResearchStatusMapResponse(statuses=svc.status_map(db, parsed))


@router.get("/albums/{album_id}", response_model=AlbumResearchResponse)
def get_album_research(
    album_id: str,
    db: Session = Depends(get_db),
    svc: ResearchService = Depends(get_research_service),
):
    # 404 when no note yet → the writer GUI shows the "조사하기" button in its place.
    row = svc.get_research(db, parse_uuid_or_404(album_id))
    if row is None:
        raise HTTPException(status_code=404, detail="No research note for this album")
    return _research_response(row)


@router.post("/albums/{album_id}", response_model=AlbumResearchResponse)
def trigger_album_research(
    album_id: str,
    req: ResearchTriggerRequest,
    db: Session = Depends(get_db),
    svc: ResearchService = Depends(get_research_service),
    _claims: Dict = Depends(require_owner),
):
    try:
        row = svc.trigger(db, parse_uuid_or_404(album_id), mode=req.mode, instruction=req.instruction)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except ResearchStateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _research_response(row)
