# app/api/routes/playback.py
"""FEAT-pocket-buckit Step 3 (D3 / OQ8) — async Spotify Web Playback SDK token mint.

GET /api/playback/spotify-token is its OWN explicit Cognito-JWT route in
infra/apigateway.tf — NOT the GET /api/{proxy+} edge_guard catch-all — so an edge-only
request (CloudFront x-origin-verify, no Bearer) is rejected at the authorizer (401) and can
never mint a streaming token. The FastAPI dependency below enforces the same in-app
(belt-and-suspenders). rule #9 holds: the handler only async-mints a short-lived token, it
never proxies a Spotify content call.
"""
import logging
from typing import Dict, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.schemas import PlaybackResolveResponse, SpotifyStreamingTokenResponse
from app.core.auth import require_cognito_token, resolve_owner
from app.db.session import get_db
from app.di import get_playback_service
from app.services.playback_service import (
    PlaybackItemNotFoundError,
    PlaybackNotConfiguredError,
    PlaybackProviderError,
    PlaybackService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/spotify-token", response_model=SpotifyStreamingTokenResponse)
def spotify_token(
    svc: PlaybackService = Depends(get_playback_service),
    claims: Dict = Depends(require_cognito_token),
):
    owner = resolve_owner(claims)  # owner from verified sub, never the body (OQ11)
    try:
        tok = svc.mint_streaming_token(owner=owner)
    except PlaybackNotConfiguredError:
        # Dormant until the Step-5 owner `streaming` OAuth consent provisions a refresh
        # token. 503 (not 500/501) = a real JWT-gated route with nothing to mint yet.
        raise HTTPException(status_code=503, detail="Spotify playback not configured")
    except PlaybackProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return SpotifyStreamingTokenResponse(
        access_token=str(tok["access_token"]),
        expires_in=int(tok["expires_in"]),  # type: ignore[arg-type]
        token_type=str(tok["token_type"]),
    )


@router.get("/resolve", response_model=PlaybackResolveResponse)
def resolve_playback_uri(
    item_type: Literal["album", "track"] = Query(..., alias="type"),
    item_id: str = Query(..., alias="id"),
    svc: PlaybackService = Depends(get_playback_service),
    db: Session = Depends(get_db),
):
    """Map a catalog DB id → a Spotify URI (spotify:album|track:<spotify_id>) for the Web
    Playback SDK (FEAT-spotify-streaming-playback Step 2). Unlike /spotify-token this is
    edge_guard-only — NO Cognito JWT, NO dedicated infra/apigateway.tf route: spotify_id is
    a public identifier and the catalog is otherwise edge_guard-only (unified search, bucket
    reads), so there is nothing to JWT-gate. rule #9 holds: a direct catalog DB read, never a
    synchronous Spotify content call. Bad type → 422 (Literal); unknown/empty id → 404."""
    try:
        uri = svc.resolve_uri(db, item_type=item_type, item_id=item_id)
    except PlaybackItemNotFoundError:
        raise HTTPException(status_code=404, detail=f"No {item_type} with id {item_id}")
    return PlaybackResolveResponse(uri=uri)
