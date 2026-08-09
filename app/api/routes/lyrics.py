# app/api/routes/lyrics.py
"""FEAT-lyrics-viewer Step 1 — member-legitimate normalized-lyrics read.

GET /api/lyrics/{spotify_track_id} is its OWN explicit Cognito-JWT route in
infra/apigateway.tf — NOT the GET /api/{proxy+} edge_guard catch-all — modeled on
/api/playback/spotify-token: an edge-only request (CloudFront x-origin-verify, no
Bearer) is rejected at the authorizer (401). The in-app guard below is the
belt-and-suspenders.

**Owner-only 2026-07-28 → 2026-08-09, reopened to any signed-in member.** The
07-28 tightening (RFC §6.9 O2) reasoned that the route now also serves the
Genius annotation store, third-party commentary attached to the owner's
private research corpus. `FEAT-playback-bucket-player` owner decision 4
(2026-08-03) reverted that call — members get the read back, annotations
included (CHORE-lyrics-member-guard-reopen). The route and its guard code
had drifted from that decision until this change closed the gap.

The translation-request POST stays owner-only — a SEPARATE, still-standing
reason: it lets a caller queue LLM work against the owner's corpus, which is
a cost/quota concern independent of who may read.

Keyed by spotify_track_id (what the live playback read returns as item.id) and resolved
server-side to the catalog track — one round trip, no separate resolve endpoint.
rule #9-safe: a direct catalog DB read, never a synchronous Spotify call.
"""
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import LyricsResponse, LyricsTranslationInfo
from app.core.auth import require_cognito_token, require_owner
from app.db.session import get_db
from app.di import get_lyrics_service
from app.services.lyrics_service import (
    LyricsNotTranslatableError,
    LyricsService,
    LyricsTrackNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{spotify_track_id}", response_model=LyricsResponse)
def get_lyrics(
    spotify_track_id: str,
    svc: LyricsService = Depends(get_lyrics_service),
    db: Session = Depends(get_db),
    claims: Dict = Depends(require_cognito_token),
):
    """Normalized lyric segments for one catalog track (any signed-in member).

    Unknown spotify_track_id → 404 (no catalog track). A known track without viewable
    lyrics is NOT a 404 — it returns availability "no_lyrics" / "unavailable" so the
    viewer can render the correct empty state.
    """
    try:
        return svc.get_normalized(db, spotify_track_id=spotify_track_id)
    except LyricsTrackNotFoundError:
        raise HTTPException(status_code=404, detail="No track with that spotify id")


@router.post("/{spotify_track_id}/translation-request", response_model=LyricsTranslationInfo)
def request_lyrics_translation(
    spotify_track_id: str,
    svc: LyricsService = Depends(get_lyrics_service),
    db: Session = Depends(get_db),
    claims: Dict = Depends(require_owner),
):
    """Enqueue a Korean-translation request for one track (FEAT-lyrics-translation).

    Upserts status='requested' — idempotent while pending, allowed again on
    done/failed/stale. The local launchd poller claims the row; no LLM call happens
    here (rule #9 spirit). Own explicit JWT route in infra/apigateway.tf like the GET —
    the POST 404s at the edge until that route is applied.
    """
    try:
        return svc.request_translation(db, spotify_track_id=spotify_track_id)
    except LyricsTrackNotFoundError:
        raise HTTPException(status_code=404, detail="No track with that spotify id")
    except LyricsNotTranslatableError:
        raise HTTPException(status_code=409, detail="Track has no translatable lyrics")
