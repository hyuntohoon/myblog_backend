# app/api/routes/integrations.py
# FEAT-multi-user-accounts Phase 3a — the member's listening/AI integrations.
#   GET    /api/integrations                    — list the caller's integrations
#                                                 (rides the edge_guard GET catch-all).
#   PUT    /api/integrations/lastfm             — connect/replace Last.fm username (JWT route).
#   DELETE /api/integrations/lastfm             — disconnect Last.fm (JWT route).
#   GET    /api/integrations/lastfm/now-playing — the caller's Last.fm now-playing.
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.routes.me import _member_id
from app.api.schemas import (
    ConnectLastfmRequest,
    IntegrationResponse,
    IntegrationsResponse,
    LastfmNowPlayingResponse,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_integration_service
from app.services.integration_service import LASTFM_PROVIDER, IntegrationService

logger = logging.getLogger(__name__)

router = APIRouter()


def _integration_response(row) -> IntegrationResponse:
    return IntegrationResponse(
        provider=row.provider,
        username=row.username,
        status=row.status,
        last_synced_at=row.last_synced_at,
    )


@router.get("", response_model=IntegrationsResponse)
def list_integrations(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    rows = svc.list_integrations(db, _member_id(claims))
    return IntegrationsResponse(integrations=[_integration_response(r) for r in rows])


@router.put("/lastfm", response_model=IntegrationResponse)
def connect_lastfm(
    payload: ConnectLastfmRequest,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    row = svc.connect_lastfm(db, _member_id(claims), claims, payload.username)
    return _integration_response(row)


@router.delete("/lastfm", status_code=204)
def disconnect_lastfm(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    svc.disconnect(db, _member_id(claims), LASTFM_PROVIDER)
    return Response(status_code=204)


@router.get("/lastfm/now-playing", response_model=LastfmNowPlayingResponse)
def lastfm_now_playing(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    row = svc.lastfm_now_playing(db, _member_id(claims))
    if row is None:
        return LastfmNowPlayingResponse(is_playing=False)
    return LastfmNowPlayingResponse(
        is_playing=True,
        artist=row.artist_name,
        track=row.track_name,
        album=row.album_name,
        image_url=row.image_url,
        played_at=row.played_at,
    )
