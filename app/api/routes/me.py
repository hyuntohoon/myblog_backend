# app/api/routes/me.py
# FEAT-multi-user-accounts Phase 0 / 0d — member self-profile.
#   GET    /api/me  — lazy-provisions the users row on first authed call (rides
#                     the API GW GET catch-all; JWT checked here at the Lambda).
#   PATCH  /api/me  — handle / display_name edits (JWT-authorizer route at API GW).
#   DELETE /api/me  — account deletion, 개인정보보호법: Cognito user first, then
#                     the DB row (JWT-authorizer route at API GW).
#   GET    /api/me/album-states — MY per-album states, private facet included
#                     (rides the GET catch-all; JWT checked here at the Lambda).
#   GET    /api/me/review-candidates — MY "평론 쓸 것" queue: marked albums joined to
#                     their identity (same catch-all, same JWT-at-the-Lambda).
#   GET    /api/me/reratings — MY open 재평가 list, withdrawn score included
#                     (same catch-all). PUT/DELETE /api/me/reratings/{album_id}
#                     start/cancel one — both are JWT-authorizer routes at API GW.
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.schemas import (
    MeResponse,
    MyAlbumStateListResponse,
    MyAlbumStateResponse,
    MyReratingListResponse,
    MyReratingResponse,
    PlannedRatingListResponse,
    PlannedRatingResponse,
    ReviewCandidateListResponse,
    ReviewCandidateResponse,
    UpdateMeRequest,
)
from app.clients.cognito_client import CognitoDeleteError, delete_cognito_user
from app.core.auth import require_cognito_token, require_owner
from app.core.config import get_settings
from app.core.ids import parse_uuid_or_404
from app.db.session import get_db
from app.di import (
    get_planned_rating_service,
    get_rating_service,
    get_rerating_service,
    get_user_service,
)
from app.services.artist_primary import primary_artist_map
from app.services.planned_rating_service import PlannedRatingService
from app.services.rating_service import AlbumNotFoundError, RatingService
from app.services.rerating_service import NoRatingToRerateError, ReratingService
from app.services.user_service import (
    LOCAL_DEV_USER_ID,
    HandleTakenError,
    UserService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _member_id(claims: Dict[str, Any] | None) -> uuid.UUID:
    """The acting member id from the verified JWT sub. local/dev claims are {}
    → the fixed all-zeros id (users.id is UUID, so the SINGLE_OWNER string
    sentinel can't be reused here). Cognito subs are UUIDs, federated included."""
    sub = (claims or {}).get("sub")
    if not sub:
        return LOCAL_DEV_USER_ID
    try:
        return uuid.UUID(sub)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")


def provisioned_member_id(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: UserService = Depends(get_user_service),
) -> uuid.UUID:
    """The acting member's id, guaranteeing their users row exists (lazy-provision
    on first authed call — a member's first bucket / to-listen write can be their
    first authed action, mirroring reviews). FEAT-multi-user Phase 2: scoped
    per-user routes depend on this instead of require_owner."""
    member_id = _member_id(claims)
    svc.get_or_create(db, member_id, claims)
    return member_id


def provisioned_owner_id(
    claims: Dict[str, Any] = Depends(require_owner),
    db: Session = Depends(get_db),
    svc: UserService = Depends(get_user_service),
) -> uuid.UUID:
    """The owner's member id (require_owner), guaranteeing the owner users row
    exists. For owner-only per-user infrastructure (the spotify_library bucket,
    which carries user_id = owner in Phase 2 — the Spotify lane is owner-only until
    Phase 3b)."""
    owner_id = _member_id(claims)
    svc.get_or_create(db, owner_id, claims)
    return owner_id


def _me_response(user) -> MeResponse:
    return MeResponse(
        id=str(user.id),
        email=user.email,
        handle=user.handle,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
    )


@router.get("", response_model=MeResponse)
def get_me(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: UserService = Depends(get_user_service),
):
    user = svc.get_or_create(db, _member_id(claims), claims)
    return _me_response(user)


@router.get("/album-states", response_model=MyAlbumStateListResponse)
def get_my_album_states(
    album_id: Optional[str] = Query(default=None),
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: RatingService = Depends(get_rating_service),
):
    """The caller's own per-album states (FEAT-album-review-authoring Step 1).

    Serves both surfaces that can carry a mark: the bucket board reads the whole
    set once, the album overlay filters to one id. Private — it carries the
    editorial marks — so it is JWT-only and scoped to the caller's own member id;
    there is deliberately no way to ask for someone else's.

    A GET needs no API Gateway route: authed GETs ride the edge_guard catch-all
    and the JWT is verified here at the Lambda, like GET /api/me. That is why
    Step 1 ships with no infra change.
    """
    aid: Optional[uuid.UUID] = None
    if album_id:
        try:
            aid = uuid.UUID(album_id)
        except ValueError:
            # A malformed filter matches nothing; it is not a server error.
            return MyAlbumStateListResponse(states=[])
    rows = svc.my_states(db, _member_id(claims), aid)
    return MyAlbumStateListResponse(
        states=[
            MyAlbumStateResponse(
                album_id=str(r.album_id),
                rating=float(r.rating) if r.rating is not None else None,
                comment=r.comment,
                review_candidate=bool(r.review_candidate),
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
    )


@router.get("/review-candidates", response_model=ReviewCandidateListResponse)
def get_my_review_candidates(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: RatingService = Depends(get_rating_service),
):
    """The caller's "평론 쓸 것" queue (FEAT-album-review-authoring Step 2).

    The harvest side of the Step 1 mark: Step 1 shipped placing and clearing it,
    this is where the marks gather into a list. Private for the same reason the
    mark is (RFC C6 — a visible mark is a promise), so it is JWT-only and scoped
    to the caller; there is no handle parameter, by design.

    Like the other authed GETs, it rides the edge_guard catch-all with the JWT
    checked here at the Lambda — no API Gateway route, no `terraform apply`.
    """
    rows = svc.my_review_candidates(db, _member_id(claims))
    # One batch resolve for the whole queue, mirroring the public member feed.
    amap = primary_artist_map(db, [a.id for _, a in rows])
    return ReviewCandidateListResponse(
        candidates=[
            ReviewCandidateResponse(
                album_id=str(r.album_id),
                album_title=a.title,
                album_cover_url=a.cover_url,
                artist_id=(amap.get(str(a.id)) or (None, None))[0],
                artist_name=(amap.get(str(a.id)) or (None, None))[1],
                rating=float(r.rating) if r.rating is not None else None,
                comment=r.comment,
                updated_at=r.updated_at,
            )
            for r, a in rows
        ]
    )


@router.get("/planned-ratings", response_model=PlannedRatingListResponse)
def get_my_planned_ratings(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: PlannedRatingService = Depends(get_planned_rating_service),
):
    """The caller's 평가 예정 ("plan to rate") queue (FEAT-rating-smart-collections
    Step 2, Option B). Strictly separate from /review-candidates — a planned
    rating carries no rating/comment, only the fact that it was planned.

    Private, JWT-only, scoped to the caller with no handle parameter — same
    posture as /review-candidates. Rides the edge_guard GET catch-all like
    every other authed GET in this file — no API Gateway route needed.
    """
    rows = svc.list_planned(db, _member_id(claims))
    amap = primary_artist_map(db, [a.id for _, a in rows])
    return PlannedRatingListResponse(
        planned=[
            PlannedRatingResponse(
                album_id=str(r.album_id),
                album_title=a.title,
                album_cover_url=a.cover_url,
                artist_id=(amap.get(str(a.id)) or (None, None))[0],
                artist_name=(amap.get(str(a.id)) or (None, None))[1],
                created_at=r.created_at,
            )
            for r, a in rows
        ]
    )


@router.put("/planned-ratings/{album_id}", status_code=204)
def mark_planned_rating(
    album_id: str,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: PlannedRatingService = Depends(get_planned_rating_service),
):
    """Mark an album as 평가 예정 — the drop target for the 평가전 tile in
    BucketBoard. Idempotent: marking twice is a no-op, not an error.
    """
    aid = parse_uuid_or_404(album_id)
    try:
        svc.mark(db, _member_id(claims), claims, aid)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    return Response(status_code=204)


@router.delete("/planned-ratings/{album_id}", status_code=204)
def unmark_planned_rating(
    album_id: str,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: PlannedRatingService = Depends(get_planned_rating_service),
):
    """Unmark. Idempotent: unmarking an album that was never planned (or
    already unmarked) is also a no-op — 204 either way, no 404.
    """
    aid = parse_uuid_or_404(album_id)
    svc.unmark(db, _member_id(claims), aid)
    return Response(status_code=204)


@router.get("/reratings", response_model=MyReratingListResponse)
def get_my_reratings(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: ReratingService = Depends(get_rerating_service),
):
    """The caller's open 재평가 list (FEAT-album-rerating).

    This is the AUTHOR-ONLY view: it carries `previous_rating`/`previous_comment`,
    the withdrawn 평가 that powers the 이전 ★ hint and the 재평가 취소 restore.
    The same rows appear publicly on GET /api/members/{handle}, but without those
    two fields — see MemberReratingResponse.

    Rides the edge_guard GET catch-all like every other authed GET here.
    """
    rows = svc.list_pending(db, _member_id(claims))
    amap = primary_artist_map(db, [a.id for _, a in rows])
    return MyReratingListResponse(
        reratings=[
            MyReratingResponse(
                album_id=str(r.album_id),
                album_title=a.title,
                album_cover_url=a.cover_url,
                artist_id=(amap.get(str(a.id)) or (None, None))[0],
                artist_name=(amap.get(str(a.id)) or (None, None))[1],
                created_at=r.created_at,
                previous_rating=float(r.previous_rating),
                previous_comment=r.previous_comment,
            )
            for r, a in rows
        ]
    )


@router.put("/reratings/{album_id}", status_code=204)
def start_rerating(
    album_id: str,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: ReratingService = Depends(get_rerating_service),
):
    """Withdraw the caller's 평가 for an album and open a 재평가.

    PUT, not POST, to match the neighbouring /planned-ratings mark: both are
    "make this state exist", idempotent — starting a 재평가 that is already open
    is a no-op, not an error.

    409 when the caller has no 평가 to withdraw: 재평가 means redoing a finished
    one, so there is nothing to redo. Distinct from the 404 for an album that
    does not exist at all.
    """
    aid = parse_uuid_or_404(album_id)
    try:
        svc.start(db, _member_id(claims), claims, aid)
    except AlbumNotFoundError:
        raise HTTPException(status_code=404, detail="Album not found")
    except NoRatingToRerateError:
        raise HTTPException(status_code=409, detail="No rating to rerate")
    return Response(status_code=204)


@router.delete("/reratings/{album_id}", status_code=204)
def cancel_rerating(
    album_id: str,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: ReratingService = Depends(get_rerating_service),
):
    """Cancel an open 재평가 — the withdrawn 평가 comes back exactly as it was.

    Idempotent: cancelling a 재평가 that is not open is a no-op, 204 either way.
    Note this is the only way BACK; completing the 재평가 (saving a new star via
    PUT /api/reviews/albums/{album_id}) ends it forward, from RatingService.
    """
    aid = parse_uuid_or_404(album_id)
    svc.cancel(db, _member_id(claims), aid)
    return Response(status_code=204)


@router.patch("", response_model=MeResponse)
def update_me(
    payload: UpdateMeRequest,
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: UserService = Depends(get_user_service),
):
    fields = {
        k: v
        for k, v in payload.model_dump(exclude_unset=True).items()
        if v is not None
    }
    try:
        user = svc.update_me(db, _member_id(claims), claims, **fields)
    except HandleTakenError:
        raise HTTPException(status_code=409, detail="Handle already taken")
    return _me_response(user)


@router.delete("", status_code=204)
def delete_me(
    claims: Dict[str, Any] = Depends(require_cognito_token),
    db: Session = Depends(get_db),
    svc: UserService = Depends(get_user_service),
):
    member_id = _member_id(claims)
    settings = get_settings()
    if settings.OWNER_SUB and str(member_id) == settings.OWNER_SUB:
        # The blog-admin identity must not be deletable through the member flow.
        raise HTTPException(status_code=403, detail="Owner account cannot be deleted")

    # Cognito first, then the DB row: issued JWTs stay valid until expiry, so if
    # the DB delete fails the client can retry — the Cognito re-lookup finds
    # nothing (idempotent) and the row delete runs again. DB-first would let a
    # failed Cognito call strand a deletable login that recreates the row.
    try:
        delete_cognito_user(str(member_id))
    except CognitoDeleteError:
        raise HTTPException(
            status_code=502, detail="Account deletion failed upstream — retry"
        )
    svc.delete_me(db, member_id)
    return Response(status_code=204)
