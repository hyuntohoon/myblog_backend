# app/api/routes/todays_pick.py
# FEAT-today-buckit Step 4 — owner-curated "song of the day" store + history.
#
# Route posture (mirrors genres/sections/reviews):
#   - GET /api/todays-pick         public (edge_guard only — no auth dependency)
#   - GET /api/todays-pick/history public (edge_guard only)
#   - PUT /api/todays-pick         owner  (require_owner)
#   - DELETE /api/todays-pick      owner  (require_owner)
#   - /api/todays-pick/queue/*     owner  (require_owner, including GET)
#
# The public GETs ride the CloudFront edge_guard (x-origin-verify) — no
# apigateway route is needed for them (Step 5 adds routes only for the writes).
# A no-pick day is a normal state: GET returns 200 + null and the home tile
# hides (it is NOT an error → not a 404).
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.schemas import (
    AddToPickQueueRequest,
    DailyPickItem,
    DailyPickQueueItem,
    UpsertTodaysPickRequest,
)
from app.core.auth import require_owner
from app.db.session import get_db
from app.di import get_todays_pick_service
from app.services.todays_pick_service import TodaysPickService

logger = logging.getLogger(__name__)

router = APIRouter()


def _item(p) -> DailyPickItem:
    """Serialize a DailyPick ORM row into the response shape, casting the UUID
    columns to str (mirrors genres.py's _node helper — Pydantic's from_attributes
    does not coerce UUID → str automatically)."""
    return DailyPickItem(
        id=str(p.id),
        pick_date=p.pick_date,
        track_id=str(p.track_id),
        album_id=str(p.album_id),
        title=p.title,
        artist=p.artist,
        cover_url=p.cover_url,
        spotify_track_id=p.spotify_track_id,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _queue_item(row) -> DailyPickQueueItem:
    """Serialize a staged queue row, including UUID-to-string coercion."""
    return DailyPickQueueItem(
        id=str(row.id),
        track_id=str(row.track_id),
        album_id=str(row.album_id),
        title=row.title,
        artist=row.artist,
        cover_url=row.cover_url,
        spotify_track_id=row.spotify_track_id,
        created_at=row.created_at,
    )


# ── reads (edge_guard only — no JWT; covered by GET /api/{proxy+}) ───────────────

@router.get("", response_model=Optional[DailyPickItem])
def get_todays_pick(
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
):
    """Today's pick, or null on a no-pick day (home tile hides on null)."""
    pick = svc.get_today(db)
    return _item(pick) if pick is not None else None


@router.get("/history", response_model=List[DailyPickItem])
def get_todays_pick_history(
    limit: int = Query(30, ge=1, le=100, description="max picks to return"),
    before: Optional[date] = Query(
        None, description="exclusive upper bound (ISO date) for paging older picks"
    ),
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
):
    """Date-desc history of past picks. `before` pages older entries."""
    return [_item(p) for p in svc.list_history(db, limit=limit, before=before)]


# ── mutations (owner only — require_owner) ──────────────────────────────────────

@router.put("", response_model=DailyPickItem)
def put_todays_pick(
    req: UpsertTodaysPickRequest,
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
    _claims: Dict = Depends(require_owner),
):
    """Set today's pick. Upserts on pick_date = today; re-POST overwrites. The
    server pins pick_date to today — the body carries the track's ids + display."""
    return _item(
        svc.upsert(
            db,
            track_id=str(req.track_id),
            album_id=str(req.album_id),
            title=req.title,
            artist=req.artist,
            cover_url=req.cover_url,
            spotify_track_id=req.spotify_track_id,
        )
    )


@router.delete("", status_code=204)
def delete_todays_pick(
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
    _claims: Dict = Depends(require_owner),
):
    """Clear today's pick ("unpost today"). 204 if a pick was removed; 404 if
    nothing was posted today (re-PUT is the normal path to replace a pick)."""
    if not svc.delete_today(db):
        raise HTTPException(status_code=404, detail="No pick posted today")
    return Response(status_code=204)


# ── private staging queue (owner only — require_owner) ─────────────────────────

@router.get("/queue", response_model=List[DailyPickQueueItem])
def get_pick_queue(
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
    _claims: Dict = Depends(require_owner),
):
    """List staged picks newest-first. This read is owner-only in-Lambda."""
    return [_queue_item(row) for row in svc.list_queue(db)]


@router.post("/queue", response_model=DailyPickQueueItem)
def add_to_pick_queue(
    req: AddToPickQueueRequest,
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
    _claims: Dict = Depends(require_owner),
):
    """Stage a track; re-adding the same track_id is a successful no-op."""
    return _queue_item(
        svc.add_to_queue(
            db,
            track_id=str(req.track_id),
            album_id=str(req.album_id),
            title=req.title,
            artist=req.artist,
            cover_url=req.cover_url,
            spotify_track_id=req.spotify_track_id,
        )
    )


@router.delete("/queue/{queue_id}", status_code=204)
def delete_from_pick_queue(
    queue_id: str,
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
    _claims: Dict = Depends(require_owner),
):
    """Remove a staged pick."""
    if not svc.delete_from_queue(db, queue_id):
        raise HTTPException(status_code=404, detail="Queued pick not found")
    return Response(status_code=204)


@router.post("/queue/{queue_id}/promote", response_model=DailyPickItem)
def promote_from_pick_queue(
    queue_id: str,
    db: Session = Depends(get_db),
    svc: TodaysPickService = Depends(get_todays_pick_service),
    _claims: Dict = Depends(require_owner),
):
    """Atomically post a staged pick as today's pick and consume the queue row."""
    pick = svc.promote_from_queue(db, queue_id)
    if pick is None:
        raise HTTPException(status_code=404, detail="Queued pick not found")
    return _item(pick)
