# app/api/routes/me.py
# FEAT-multi-user-accounts Phase 0 / 0d — member self-profile.
#   GET    /api/me  — lazy-provisions the users row on first authed call (rides
#                     the API GW GET catch-all; JWT checked here at the Lambda).
#   PATCH  /api/me  — handle / display_name edits (JWT-authorizer route at API GW).
#   DELETE /api/me  — account deletion, 개인정보보호법: Cognito user first, then
#                     the DB row (JWT-authorizer route at API GW).
import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.schemas import MeResponse, UpdateMeRequest
from app.clients.cognito_client import CognitoDeleteError, delete_cognito_user
from app.core.auth import require_cognito_token, require_owner
from app.core.config import get_settings
from app.db.session import get_db
from app.di import get_user_service
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
