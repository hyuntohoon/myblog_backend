# app/api/routes/admin_lyrics.py
"""FEAT-lyrics-match-correction Step 3a — the owner's lyrics candidate panel.

Route posture: all three are owner-only (require_owner, in-Lambda) AND each has its own
Cognito-JWT route in infra/apigateway.tf, like GET /api/lyrics/{id}: the panel carries
lyric previews, which share track_lyrics' never-public bar, so even the GET does not ride
the edge_guard GET catch-all. Until those routes are applied the API answers 404 at the
edge (CLAUDE.md: a new protected route returning 404 is usually a missing apigateway.tf
route).

  - GET  /api/admin/lyrics/{spotify_track_id}/candidates
  - POST /api/admin/lyrics/{spotify_track_id}/candidates/refresh
  - POST /api/admin/lyrics/{spotify_track_id}/decision

None of them calls LRCLIB: refresh and pick enqueue worker jobs, the other decisions are
short DB writes committed before any SQS send.
"""
from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.schemas import (
    AdminLyricsCandidates,
    AdminLyricsDecisionRequest,
    AdminLyricsDecisionResult,
    AdminLyricsRefreshResult,
)
from app.core.authz import require_owner, resolve_owner
from app.db.session import get_db
from app.di import get_lyrics_admin_service
from app.services.lyrics_admin_service import (
    LyricsAdminConflict,
    LyricsAdminQueueUnavailable,
    LyricsAdminService,
    LyricsAdminTrackNotFound,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_NOT_FOUND = "No track with that spotify id"


@router.get("/{spotify_track_id}/candidates", response_model=AdminLyricsCandidates)
def get_lyrics_candidates(
    spotify_track_id: str,
    _claims: Dict = Depends(require_owner),
    svc: LyricsAdminService = Depends(get_lyrics_admin_service),
    db: Session = Depends(get_db),
):
    """The stored candidate set with each candidate's verdict, what the track serves now and
    its lock. Opening it starts nothing; `stale` says when a refresh is due."""
    try:
        return svc.candidates(db, spotify_track_id=spotify_track_id)
    except LyricsAdminTrackNotFound:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)


@router.post(
    "/{spotify_track_id}/candidates/refresh",
    response_model=AdminLyricsRefreshResult,
    status_code=202,
    responses={
        404: {"description": "Unknown spotify track id"},
        503: {"description": "Queue unavailable; nothing was enqueued"},
    },
)
def refresh_lyrics_candidates(
    spotify_track_id: str,
    _claims: Dict = Depends(require_owner),
    svc: LyricsAdminService = Depends(get_lyrics_admin_service),
    db: Session = Depends(get_db),
):
    """Enqueue one re-check of this track now. The worker searches LRCLIB, re-judges the
    track under the running matcher (a locked row is never rewritten) and stores the set."""
    try:
        return AdminLyricsRefreshResult(enqueued=svc.refresh(db, spotify_track_id=spotify_track_id))
    except LyricsAdminTrackNotFound:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    except LyricsAdminQueueUnavailable:
        raise HTTPException(status_code=503, detail="Queue unavailable; refresh again")


@router.post(
    "/{spotify_track_id}/decision",
    response_model=AdminLyricsDecisionResult,
    responses={
        404: {"description": "Unknown spotify track id"},
        409: {"description": "pick: not in the stored candidate set, or rejected for this track"},
        503: {"description": "Database or queue unavailable; the decision may be resent"},
    },
)
def post_lyrics_decision(
    spotify_track_id: str,
    body: AdminLyricsDecisionRequest,
    claims: Dict = Depends(require_owner),
    svc: LyricsAdminService = Depends(get_lyrics_admin_service),
    db: Session = Depends(get_db),
):
    """Apply the owner's decision. `pick` is enqueued (the worker re-fetches the body and
    writes it locked); `no_lyrics` locks the track as having none; `reject` records a
    candidate as never-again for this track and takes it down if it is the one served;
    `unlock` hands the track back to the automatic jobs. Every decision is idempotent to
    resend."""
    try:
        return svc.decide(
            db, spotify_track_id=spotify_track_id, action=body.action,
            lrclib_id=body.lrclib_id, actor=resolve_owner(claims),
        )
    except LyricsAdminTrackNotFound:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    except LyricsAdminConflict as conflict:
        raise HTTPException(status_code=409, detail=conflict.code)
    except LyricsAdminQueueUnavailable:
        raise HTTPException(status_code=503, detail="Queue unavailable; resend the pick")
    except SQLAlchemyError as error:
        db.rollback()
        logger.error("lyrics decision: DB write failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Decision unavailable; resend it")
