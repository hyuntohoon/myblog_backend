# app/api/routes/playback.py
"""FEAT-member-player Step 2 — per-member Spotify Web Playback SDK token mint.

GET /api/playback/spotify-token is its OWN explicit Cognito-JWT route in
infra/apigateway.tf — NOT the GET /api/{proxy+} edge_guard catch-all — so an edge-only
request (CloudFront x-origin-verify, no Bearer) is rejected at the authorizer (401) and can
never mint a streaming token. The FastAPI dependency below enforces the same in-app
(belt-and-suspenders). Tokens are minted per member from the verified JWT sub's row-scoped
integration; the owner (and the local/dev empty-claims bypass) remains a special case on
the existing owner credential path. rule #9 holds: this auth-host mint never proxies a
Spotify content call.
"""
import logging
from typing import Dict, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes.me import _member_id
from app.api.schemas import PlaybackResolveResponse, SpotifyStreamingTokenResponse
from app.core.auth import require_cognito_token
from app.core.authz import resolve_owner
from app.core.config import settings
from app.db.session import get_db
from app.di import get_playback_service
from app.services.playback_service import (
    PlaybackItemNotFoundError,
    PlaybackNotConfiguredError,
    PlaybackNotConnectedError,
    PlaybackProviderError,
    PlaybackService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/spotify-token", response_model=SpotifyStreamingTokenResponse)
def spotify_token(
    svc: PlaybackService = Depends(get_playback_service),
    claims: Dict = Depends(require_cognito_token),
    db: Session = Depends(get_db),
):
    # Empty claims only exist under the ENV=local|dev auth bypass — a verified prod JWT
    # always carries a sub, so the owner special-case never captures an anonymous caller.
    is_owner = not claims or bool(
        settings.OWNER_SUB and claims.get("sub") == settings.OWNER_SUB
    )
    try:
        if is_owner:
            tok = svc.mint_streaming_token(owner=resolve_owner(claims))
        else:
            tok = svc.mint_member_streaming_token(db, member_id=_member_id(claims))
    except PlaybackNotConnectedError:
        raise HTTPException(status_code=404, detail="Spotify not connected")
    except PlaybackNotConfiguredError:
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
    provider: Literal["spotify", "youtube"] = Query("spotify"),
    svc: PlaybackService = Depends(get_playback_service),
    db: Session = Depends(get_db),
):
    """Map a catalog DB id → a playback URI for the requested provider.

    Default and historical behaviour: a Spotify URI (spotify:album|track:<spotify_id>) for
    the Web Playback SDK (FEAT-spotify-streaming-playback Step 2). `provider` is OPTIONAL
    and defaults to 'spotify', so every caller that predates
    FEAT-youtube-playback-provider Step A1 is unaffected — the parameter is additive, which
    is why this needs no contract-breaking change and no frontend change to keep working.

    `provider=youtube` reads the Step-A1 `track_provider_refs` mapping and returns
    youtube:video:<videoId>. Track-only (YouTube has no album-context equivalent), and only
    while the mapping is still playable — see PlaybackService.resolve_uri.

    Unlike /spotify-token this is edge_guard-only — NO Cognito JWT, NO dedicated
    infra/apigateway.tf route: spotify_id is a public identifier and the catalog is
    otherwise edge_guard-only (unified search, bucket reads), so there is nothing to
    JWT-gate. A YouTube videoId is public in the same sense, and the mapping table holds no
    per-member data, so the gating does not change here either. rule #9 holds: a direct
    catalog DB read, never a synchronous provider content call. Bad type or bad provider →
    422 (Literal); unknown/empty id, or a track with no usable mapping → 404."""
    try:
        uri = svc.resolve_uri(
            db, item_type=item_type, item_id=item_id, provider=provider
        )
    except PlaybackItemNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"No {provider} {item_type} with id {item_id}",
        )
    return PlaybackResolveResponse(uri=uri)
