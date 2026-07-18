# app/api/routes/integrations.py
# FEAT-multi-user-accounts Phase 3a/3b — the member's listening/AI integrations.
#   GET    /api/integrations                    — list the caller's integrations
#                                                 (rides the edge_guard GET catch-all).
#   PUT    /api/integrations/lastfm             — connect/replace Last.fm username (JWT route).
#   DELETE /api/integrations/lastfm             — disconnect Last.fm (JWT route).
#   GET    /api/integrations/lastfm/now-playing — the caller's Last.fm now-playing.
#   PUT    /api/integrations/spotify            — 3b-c: server-side code exchange,
#                                                 KMS-enveloped refresh-token custody (JWT route).
#   DELETE /api/integrations/spotify            — disconnect Spotify (JWT route).
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.routes.me import _member_id
from app.api.schemas import (
    ConnectLastfmRequest,
    ConnectSpotifyRequest,
    IntegrationResponse,
    IntegrationsResponse,
    LastfmNowPlayingResponse,
)
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_integration_service
from app.services.integration_service import (
    LASTFM_PROVIDER,
    SPOTIFY_PROVIDER,
    IntegrationService,
    SpotifyCodeRejectedError,
    SpotifyConnectNotConfiguredError,
    SpotifyProviderError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _spotify_scope(row) -> Optional[str]:
    if row.provider != SPOTIFY_PROVIDER:
        return None
    payload = getattr(row, "payload", None)
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    scope = parsed.get("scope")
    return scope if isinstance(scope, str) else None


def _integration_response(row) -> IntegrationResponse:
    return IntegrationResponse(
        provider=row.provider,
        username=row.username,
        status=row.status,
        last_synced_at=row.last_synced_at,
        scope=_spotify_scope(row),
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


@router.put("/spotify", response_model=IntegrationResponse)
def connect_spotify(
    payload: ConnectSpotifyRequest,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    """FEAT-multi-user 3b-c: exchange the callback `?code` server-side and store
    the KMS-enveloped refresh token. Rule #9: a member-initiated mutation against
    the Spotify AUTH host (the playback-mint exception) — no sync content call.
    Fail-closed: missing KMS key / app creds ⇒ 503 before any outbound call."""
    try:
        row = svc.connect_spotify(db, _member_id(claims), claims, payload.code)
    except SpotifyConnectNotConfiguredError:
        raise HTTPException(
            status_code=503, detail="Spotify member connect not configured"
        )
    except SpotifyCodeRejectedError:
        raise HTTPException(
            status_code=400,
            detail="Spotify rejected the authorization code (expired or already used)",
        )
    except SpotifyProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _integration_response(row)


@router.delete("/spotify", status_code=204)
def disconnect_spotify(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: IntegrationService = Depends(get_integration_service),
):
    svc.disconnect(db, _member_id(claims), SPOTIFY_PROVIDER)
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
