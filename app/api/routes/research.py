# app/api/routes/research.py
# FEAT-album-research-notes Step 4 — writer-facing AI research notes (never public).
#
# Notes belong to the ALBUM, not the surface — the same two routes back the BucketBoard
# item and the /write editor. POST (manual trigger / restart / refine) is Cognito-JWT at
# the API Gateway (matching infra/apigateway.tf route). GET rides the edge_guard catch-all
# (api_get_proxy) like GET /api/buckets — no JWT route; reachable only via CloudFront.
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import AlbumResearchResponse, ResearchTriggerRequest
from app.core.auth import require_cognito_token
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


@router.get("/albums/{album_id}", response_model=AlbumResearchResponse)
def get_album_research(
    album_id: str,
    db: Session = Depends(get_db),
    svc: ResearchService = Depends(get_research_service),
):
    # 404 when no note yet → the writer GUI shows the "조사하기" button in its place.
    row = svc.get_research(db, album_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No research note for this album")
    return _research_response(row)


@router.post("/albums/{album_id}", response_model=AlbumResearchResponse)
def trigger_album_research(
    album_id: str,
    req: ResearchTriggerRequest,
    db: Session = Depends(get_db),
    svc: ResearchService = Depends(get_research_service),
    _claims: Dict = Depends(require_cognito_token),
):
    try:
        row = svc.trigger(db, album_id, mode=req.mode, instruction=req.instruction)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except ResearchStateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _research_response(row)
