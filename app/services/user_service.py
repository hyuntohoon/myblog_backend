# app/services/user_service.py
# FEAT-multi-user-accounts Phase 0 / 0d — member identity over the V36 `users`
# table. Rows are LAZY-CREATED on a member's first authed /api/me call (decision
# 2026-07-07: no post-confirmation Lambda), keyed on the verified-JWT Cognito
# `sub`. See docs/rfcs/FEAT-multi-user-accounts.md.
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from myblog_shared_db.models import User

logger = logging.getLogger(__name__)

# Mirrors ck_users_handle_format (V36): lowercase, 3–30 chars, [a-z0-9_-] body,
# alphanumeric first/last char.
HANDLE_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,28}[a-z0-9]$"
_HANDLE_RE = re.compile(HANDLE_PATTERN)

# local/dev: require_cognito_token returns {} (no sub). /api/me still needs a
# member id to exercise the flow locally, so a fixed all-zeros id stands in
# (the string-sentinel SINGLE_OWNER can't be a users.id — that column is UUID).
LOCAL_DEV_USER_ID = uuid.UUID(int=0)


class UserNotFoundError(Exception):
    """Raised when the member row is missing. Route maps to 404."""


class HandleTakenError(Exception):
    """Raised on a uq_users_handle collision. Route maps to 409."""


def derive_handle(claims: Dict[str, Any], member_id: uuid.UUID) -> str:
    """IdP-derived default handle (OQ1): the email local part normalized into
    ck_users_handle_format, else a sub-derived fallback. Access-token bearers
    carry no email claim, so the fallback is the common prod path until the
    settings page (0e) lets the member pick a handle."""
    fallback = f"user-{member_id.hex[:8]}"
    email = claims.get("email")
    if not email or "@" not in email:
        return fallback
    base = email.split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9_-]", "-", base)
    base = re.sub(r"[-_]{2,}", "-", base).strip("-_")
    base = base[:30].rstrip("-_")
    if not _HANDLE_RE.fullmatch(base):
        return fallback
    return base


def _default_display_name(claims: Dict[str, Any], handle: str) -> str:
    name = claims.get("name") or claims.get("nickname")
    if name:
        return str(name)[:80]
    email = claims.get("email")
    if email and "@" in email:
        return email.split("@", 1)[0][:80]
    return handle


class UserService:
    def get_or_create(
        self, db: Session, member_id: uuid.UUID, claims: Optional[Dict[str, Any]]
    ) -> User:
        """The member row for the verified sub, provisioned on first call.

        Handle collision (uq_users_handle) retries once with a deterministic
        sub-derived suffix; a concurrent first-call race on the same id lands in
        the same IntegrityError branch and resolves by re-reading the row.
        """
        user = db.get(User, member_id)
        if user is not None:
            return user

        claims = claims or {}
        handle = derive_handle(claims, member_id)
        suffixed = f"{handle[:24].rstrip('-_')}-{member_id.hex[:5]}"
        email = claims.get("email")
        avatar_url = claims.get("picture")
        display_name = _default_display_name(claims, handle)

        for candidate in (handle, suffixed):
            user = User(
                id=member_id,
                email=email,
                handle=candidate,
                display_name=display_name,
                avatar_url=avatar_url,
            )
            db.add(user)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.get(User, member_id)
                if existing is not None:  # lost a same-id race — that row wins
                    return existing
                continue  # handle collision — retry with the suffixed candidate
            db.refresh(user)
            logger.info("provisioned member %s (handle=%s)", member_id, candidate)
            return user

        # Both candidates collided while the id stayed absent — the suffixed
        # candidate embeds the sub, so this is effectively unreachable.
        raise HandleTakenError(handle)

    def update_me(
        self,
        db: Session,
        member_id: uuid.UUID,
        claims: Optional[Dict[str, Any]],
        *,
        handle: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> User:
        """Apply profile edits. Lazy-creates first (a PATCH may be the member's
        first authed call), so the row always exists."""
        user = self.get_or_create(db, member_id, claims)
        if handle is not None:
            user.handle = handle
        if display_name is not None:
            user.display_name = display_name
        user.updated_at = func.now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HandleTakenError(handle or "")
        db.refresh(user)
        return user

    def delete_me(self, db: Session, member_id: uuid.UUID) -> bool:
        """Delete the member row. Idempotent — False when already gone. Owned
        rows come in Phases 2–3 (no user_id column exists anywhere yet)."""
        user = db.get(User, member_id)
        if user is None:
            return False
        db.delete(user)
        db.commit()
        logger.info("deleted member row %s", member_id)
        return True
