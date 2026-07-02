# app/api/routes/lyrics.py
"""FEAT-lyrics-viewer Step 1 — authenticated-only normalized-lyrics read.

GET /api/lyrics/{spotify_track_id} is its OWN explicit Cognito-JWT route in
infra/apigateway.tf — NOT the GET /api/{proxy+} edge_guard catch-all — modeled on
/api/playback/spotify-token: an edge-only request (CloudFront x-origin-verify, no
Bearer) is rejected at the authorizer (401). The in-app require_cognito_token below is
the belt-and-suspenders. Lyrics carry the corpus's "never in any shared response" bar,
deliberately stricter than the edge_guard-only /api/library/* tier (D28 split).

Keyed by spotify_track_id (what the live playback read returns as item.id) and resolved
server-side to the catalog track — one round trip, no separate resolve endpoint.
rule #9-safe: a direct catalog DB read, never a synchronous Spotify call.
"""
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import LyricsResponse
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_lyrics_service
from app.services.lyrics_service import LyricsService, LyricsTrackNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{spotify_track_id}", response_model=LyricsResponse)
def get_lyrics(
    spotify_track_id: str,
    svc: LyricsService = Depends(get_lyrics_service),
    db: Session = Depends(get_db),
    claims: Dict = Depends(require_cognito_token),
):
    """Normalized lyric segments for one catalog track (authenticated users only).

    Unknown spotify_track_id → 404 (no catalog track). A known track without viewable
    lyrics is NOT a 404 — it returns availability "no_lyrics" / "unavailable" so the
    viewer can render the correct empty state.
    """
    try:
        return svc.get_normalized(db, spotify_track_id=spotify_track_id)
    except LyricsTrackNotFoundError:
        raise HTTPException(status_code=404, detail="No track with that spotify id")
