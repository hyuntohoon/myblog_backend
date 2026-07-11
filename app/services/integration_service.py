# app/services/integration_service.py
# FEAT-multi-user-accounts Phase 3a — listening/AI integrations (Last.fm this step).
# user_integrations is the single connect store (V41); Last.fm uses username-only
# (public reads, no OAuth). Mirrors ReviewService: holds a UserService so connecting
# can be a member's first-ever authed action (lazy-provision).
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from myblog_shared_db.models import LastfmRecentTrack, User, UserIntegration

from app.services.review_service import MemberNotFoundError
from app.services.user_service import UserService

LASTFM_PROVIDER = "lastfm"


class IntegrationService:
    def __init__(self, users: Optional[UserService] = None):
        self._users = users or UserService()

    def list_integrations(
        self, db: Session, member_id: uuid.UUID
    ) -> List[UserIntegration]:
        return list(
            db.scalars(
                select(UserIntegration)
                .where(UserIntegration.user_id == member_id)
                .order_by(UserIntegration.provider)
            )
        )

    def connect_lastfm(
        self,
        db: Session,
        member_id: uuid.UUID,
        claims: Optional[Dict[str, Any]],
        username: str,
    ) -> UserIntegration:
        """Store/replace the member's Last.fm username. Rule-#9 principle: NO
        synchronous Last.fm call here — the worker validates the handle on its first
        poll (a bad handle → status transitions to 'error'), so a user-facing write
        never blocks on an external API. Idempotent on (user_id, provider)."""
        user = self._users.get_or_create(db, member_id, claims)
        username = username.strip()
        row = db.scalar(
            select(UserIntegration).where(
                UserIntegration.user_id == user.id,
                UserIntegration.provider == LASTFM_PROVIDER,
            )
        )
        if row is None:
            row = UserIntegration(
                user_id=user.id,
                provider=LASTFM_PROVIDER,
                username=username,
                status="connected",
            )
            db.add(row)
        else:
            row.username = username
            row.status = "connected"
        db.commit()
        db.refresh(row)
        return row

    def disconnect(self, db: Session, member_id: uuid.UUID, provider: str) -> bool:
        """Remove the member's integration for a provider. Idempotent (returns False
        if there was nothing to disconnect). Scrobble history is left in place (it is
        the member's own data and cascades on account deletion)."""
        row = db.scalar(
            select(UserIntegration).where(
                UserIntegration.user_id == member_id,
                UserIntegration.provider == provider,
            )
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True

    def lastfm_now_playing(
        self, db: Session, member_id: uuid.UUID
    ) -> Optional[LastfmRecentTrack]:
        """The member's current Last.fm now-playing row (worker-written), or None."""
        return db.scalar(
            select(LastfmRecentTrack).where(
                LastfmRecentTrack.user_id == member_id,
                LastfmRecentTrack.is_now_playing.is_(True),
            )
        )

    def public_now_playing(
        self, db: Session, handle: str
    ) -> Optional[LastfmRecentTrack]:
        """Handle-scoped public read of a member's now-playing row. Raises
        MemberNotFoundError for an unknown handle (route maps to 404); a member
        without a Last.fm integration and one with nothing playing are both
        plain None — the public profile hides the section either way, and not
        distinguishing them keeps integration status private."""
        user = db.scalar(select(User).where(User.handle == handle.lower()))
        if user is None:
            raise MemberNotFoundError(handle)
        return self.lastfm_now_playing(db, user.id)
