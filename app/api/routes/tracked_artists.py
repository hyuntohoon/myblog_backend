"""Buckit import preview and member-scoped tracked-artist CRUD."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.routes.me import provisioned_member_id
from app.api.schemas import (
    AddTrackedArtistsRequest,
    AddTrackedArtistsResponse,
    TrackedArtistCandidate,
    TrackedArtistPreviewRequest,
    TrackedArtistPreviewResponse,
    TrackedArtistResponse,
)
from app.core.config import get_settings
from app.db.session import get_db
from app.di import get_bucket_service, get_tracked_artist_service
from app.services.bucket_service import BucketNotFoundError, BucketService
from app.services.tracked_artist_service import (
    ArtistNotFoundError,
    TrackedArtistRateLimitError,
    TrackedArtistService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/preview", response_model=TrackedArtistPreviewResponse)
def preview_tracked_artists(
    req: TrackedArtistPreviewRequest,
    db: Session = Depends(get_db),
    bucket_svc: BucketService = Depends(get_bucket_service),
    tracked_svc: TrackedArtistService = Depends(get_tracked_artist_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        artists = bucket_svc.bucket_catalog_artists(db, member_id, req.bucket_id)
    except BucketNotFoundError:
        raise HTTPException(status_code=404, detail="Bucket not found")

    tracked_ids = tracked_svc.tracked_artist_ids(
        db, member_id, (artist.id for artist in artists)
    )
    return TrackedArtistPreviewResponse(
        candidates=[
            TrackedArtistCandidate(
                artist_id=artist.id,
                name=artist.name,
                photo_url=artist.photo_url,
                already_tracked=artist.id in tracked_ids,
            )
            for artist in artists
        ]
    )


@router.post("", response_model=AddTrackedArtistsResponse)
def add_tracked_artists(
    req: AddTrackedArtistsRequest,
    db: Session = Depends(get_db),
    svc: TrackedArtistService = Depends(get_tracked_artist_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    try:
        added, already_tracked = svc.add_tracks(
            db,
            member_id,
            req.artist_ids,
            daily_cap=get_settings().TRACKED_ARTIST_DAILY_CAP,
        )
    except ArtistNotFoundError:
        raise HTTPException(status_code=404, detail="Artist not found")
    except TrackedArtistRateLimitError:
        raise HTTPException(
            status_code=429,
            detail="Daily tracked artist limit reached — try again later",
        )
    return AddTrackedArtistsResponse(
        added=added,
        already_tracked=already_tracked,
    )


@router.get("", response_model=list[TrackedArtistResponse])
def list_tracked_artists(
    db: Session = Depends(get_db),
    svc: TrackedArtistService = Depends(get_tracked_artist_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    return [
        TrackedArtistResponse(
            artist_id=track.artist_id,
            name=artist.name,
            photo_url=artist.photo_url,
            added_at=track.added_at,
        )
        for track, artist in svc.list_tracks(db, member_id)
    ]


@router.delete("/{artist_id}", status_code=204)
def delete_tracked_artist(
    artist_id: uuid.UUID,
    db: Session = Depends(get_db),
    svc: TrackedArtistService = Depends(get_tracked_artist_service),
    member_id: uuid.UUID = Depends(provisioned_member_id),
):
    if not svc.delete_track(db, member_id, artist_id):
        raise HTTPException(status_code=404, detail="Tracked artist not found")
    return Response(status_code=204)
