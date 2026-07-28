# app/api/routes/lyrics.py
"""FEAT-lyrics-viewer Step 1 — OWNER-ONLY normalized-lyrics read.

GET /api/lyrics/{spotify_track_id} is its OWN explicit Cognito-JWT route in
infra/apigateway.tf — NOT the GET /api/{proxy+} edge_guard catch-all — modeled on
/api/playback/spotify-token: an edge-only request (CloudFront x-origin-verify, no
Bearer) is rejected at the authorizer (401). The in-app guard below is the
belt-and-suspenders. Lyrics carry the corpus's "never in any shared response" bar,
deliberately stricter than the edge_guard-only /api/library/* tier (D28 split).

**Owner-only since 2026-07-28, tightened from require_cognito_token.** This route
was classified member-legitimate in FEAT-multi-user 0c, when it served only lyric
text. It now also serves the Genius annotation store (FEAT-lyrics-annotations
Thread 1) — third-party commentary attached to the owner's private research corpus
— and `track_lyrics` is documented owner-only research data. Owner decision, RFC
§6.9 O2.

This is a REGRESSION for existing members by design: a signed-in non-owner could
read this until today and now gets 403. The frontend renders that as an explicit
"owner only" state rather than an error, because a member reaching it is expected,
not exceptional.

The translation-request POST is tightened the same way and for a stronger reason:
it makes a member able to queue LLM work against the owner's corpus.

Keyed by spotify_track_id (what the live playback read returns as item.id) and resolved
server-side to the catalog track — one round trip, no separate resolve endpoint.
rule #9-safe: a direct catalog DB read, never a synchronous Spotify call.
"""
import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import LyricsResponse, LyricsTranslationInfo
from app.core.auth import require_owner
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
    claims: Dict = Depends(require_owner),
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
