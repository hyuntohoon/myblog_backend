# app/services/integration_service.py
# FEAT-multi-user-accounts Phase 3a/3b — listening/AI integrations.
# user_integrations is the single connect store (V41); Last.fm uses username-only
# (public reads, no OAuth); Spotify (3b-c) stores a KMS-enveloped refresh token in
# `payload`. Mirrors ReviewService: holds a UserService so connecting can be a
# member's first-ever authed action (lazy-provision).
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from myblog_shared_db.models import (
    LastfmRecentTrack,
    SpotifyMemberNowPlaying,
    User,
    UserIntegration,
)

from app.core import kms_envelope
from app.core.config import settings
from app.services.review_service import MemberNotFoundError
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

LASTFM_PROVIDER = "lastfm"
SPOTIFY_PROVIDER = "spotify"

# accounts.spotify.com is the AUTH host (code/token exchange), NOT the Web API
# content host — the rule-#9-blessed exception (same constant as PlaybackService).
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"


class PublicNowPlaying(NamedTuple):
    """Source-merged public now-playing pick (see public_now_playing).
    source_username is set ONLY for source='lastfm' — the unverified-username
    provenance the public surface must display."""

    source: str  # 'lastfm' | 'spotify'
    artist: Optional[str]
    track: Optional[str]
    album: Optional[str]
    image_url: Optional[str]
    played_at: Optional[datetime]
    source_username: Optional[str]


class SpotifyConnectNotConfiguredError(Exception):
    """KMS key id or Spotify app creds not provisioned — the route maps this to
    503 fail-closed. Expected until the 3b-a CMK is terraform-applied. Raised
    BEFORE any outbound call so the member's one-time code is not burned, and
    BEFORE any DB write so plaintext is never stored."""


class SpotifyCodeRejectedError(Exception):
    """Spotify 4xx'd the authorization-code exchange (bad/expired/reused code or
    redirect_uri mismatch) — the route maps this to 400."""


class SpotifyProviderError(Exception):
    """Spotify auth endpoint unreachable / 5xx / malformed response — 502."""


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

    # ── Spotify (3b-c): server-side code exchange + KMS token custody ──

    def _spotify_app_creds(self) -> tuple[str, str]:
        """(client_id, client_secret) — env/settings override first (local/test
        determinism), then the myblog/spotify secret (SSM preferred; reuses
        PlaybackService's reader). Either missing ⇒ fail-closed, no exchange."""
        cid = settings.SPOTIFY_CLIENT_ID
        csec = settings.SPOTIFY_CLIENT_SECRET
        if cid and csec:
            return cid, csec
        from app.services.playback_service import PlaybackService

        payload = PlaybackService()._read_spotify_secret()
        return (
            cid or payload.get("client_id", ""),
            csec or payload.get("client_secret", ""),
        )

    def _kms_encrypt(self, plaintext: str) -> str:
        """KMS-encrypt → base64 ciphertext. ANY failure (key not yet applied,
        AccessDenied, network) ⇒ SpotifyConnectNotConfiguredError (503) — never
        store plaintext, never silently skip encryption. The error is logged
        without the plaintext or the ciphertext."""
        try:
            return kms_envelope.kms_encrypt_b64(plaintext)
        except Exception as e:
            logger.error("KMS encrypt for spotify connect failed: %s", type(e).__name__)
            raise SpotifyConnectNotConfiguredError() from None

    def connect_spotify(
        self,
        db: Session,
        member_id: uuid.UUID,
        claims: Optional[Dict[str, Any]],
        code: str,
        client: Optional[httpx.Client] = None,
    ) -> UserIntegration:
        """Exchange the member's authorization code server-side, KMS-encrypt the
        refresh token, upsert the provider='spotify' row. Rule #9: this is a
        member-initiated MUTATION against the Spotify *auth* host (the blessed
        playback-mint exception), never a content call on a read path.

        Fail-closed order: config gates FIRST (503 before any outbound call —
        the one-time code survives a dormant deploy), then the exchange, then
        KMS, and only then the DB write. Tokens are never logged or returned;
        `payload` holds {v, ciphertext (b64 KMS envelope of the refresh token),
        scope, expires_in, obtained_at} — ciphertext only for the token itself.
        """
        if not settings.USER_TOKENS_KMS_KEY_ID:
            raise SpotifyConnectNotConfiguredError()
        client_id, client_secret = self._spotify_app_creds()
        if not client_id or not client_secret:
            raise SpotifyConnectNotConfiguredError()

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.SPOTIFY_MEMBER_REDIRECT_URI,
        }
        owns_client = client is None
        client = client or httpx.Client(timeout=10)
        try:
            resp = client.post(
                _SPOTIFY_TOKEN_URL, data=data, auth=(client_id, client_secret)
            )
        except httpx.HTTPError as e:
            raise SpotifyProviderError(f"Spotify token endpoint unreachable: {e}")
        finally:
            if owns_client:
                client.close()

        if 400 <= resp.status_code < 500:
            # invalid_grant / redirect_uri mismatch — logged WITHOUT the body
            # (Spotify error bodies are safe, but stay conservative).
            logger.warning(
                "Spotify code exchange rejected: status=%s", resp.status_code
            )
            raise SpotifyCodeRejectedError()
        if resp.status_code != 200:
            logger.warning("Spotify code exchange failed: status=%s", resp.status_code)
            raise SpotifyProviderError("Spotify token exchange failed")
        try:
            body = resp.json()
        except ValueError:
            raise SpotifyProviderError("Spotify token response was not valid JSON")
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            raise SpotifyProviderError("Spotify token response missing refresh_token")

        ciphertext = self._kms_encrypt(refresh_token)
        payload = json.dumps(
            {
                "v": 1,
                "ciphertext": ciphertext,
                "scope": body.get("scope", ""),
                "expires_in": int(body.get("expires_in", 3600)),
                "obtained_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        user = self._users.get_or_create(db, member_id, claims)
        row = db.scalar(
            select(UserIntegration).where(
                UserIntegration.user_id == user.id,
                UserIntegration.provider == SPOTIFY_PROVIDER,
            )
        )
        if row is None:
            row = UserIntegration(
                user_id=user.id,
                provider=SPOTIFY_PROVIDER,
                payload=payload,
                status="connected",
            )
            db.add(row)
        else:
            row.payload = payload
            row.status = "connected"
        db.commit()
        db.refresh(row)
        return row

    def mint_member_access_token(
        self,
        db: Session,
        member_id: uuid.UUID,
        client: Optional[httpx.Client] = None,
    ) -> Dict[str, object]:
        """Exchange the member's KMS-enveloped refresh token → a short-lived access
        token (FEAT-member-player Step 2 — the member half of GET
        /api/playback/spotify-token; the owner special case stays in PlaybackService).

        Fail-closed order mirrors connect_spotify: config gates FIRST (KMS key, app
        creds → SpotifyConnectNotConfiguredError/503 before any outbound call), then
        the row-scoped custody checks (no connected row / unusable payload → the same
        503 — "nothing to mint for this member"), then decrypt, then the refresh
        grant. rule #9: an auth-host mint, never a content call. Tokens and
        ciphertext are never logged or returned beyond the access token itself.

        Rotation twin (worker spotify_member_sync_service._rotated_payload — keep in
        sync, cross-repo sweep rule): a differing response refresh_token is
        re-encrypted and persisted with scope/expires_in tracking the response when
        present; a rotation-encrypt failure keeps the old payload and still returns
        the minted access token. An `invalid_grant` rejection (revoked/rotated-away
        token) flags the row status='error' so the integrations tab can surface a
        reconnect, then still raises SpotifyProviderError (502)."""
        if not settings.USER_TOKENS_KMS_KEY_ID:
            raise SpotifyConnectNotConfiguredError()
        client_id, client_secret = self._spotify_app_creds()
        if not client_id or not client_secret:
            raise SpotifyConnectNotConfiguredError()

        row = db.scalar(
            select(UserIntegration).where(
                UserIntegration.user_id == member_id,
                UserIntegration.provider == SPOTIFY_PROVIDER,
            )
        )
        if row is None or row.status != "connected" or not row.payload:
            raise SpotifyConnectNotConfiguredError()
        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError):
            raise SpotifyConnectNotConfiguredError() from None
        if not isinstance(payload, dict):
            raise SpotifyConnectNotConfiguredError()
        ciphertext = payload.get("ciphertext")
        if not isinstance(ciphertext, str) or not ciphertext:
            raise SpotifyConnectNotConfiguredError()

        try:
            refresh_token = kms_envelope.kms_decrypt_b64(ciphertext)
        except Exception as e:
            logger.error("KMS decrypt for spotify playback failed: %s", type(e).__name__)
            raise SpotifyConnectNotConfiguredError() from None

        owns_client = client is None
        http_client = client or httpx.Client(timeout=10)
        try:
            resp = http_client.post(
                _SPOTIFY_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                auth=(client_id, client_secret),
            )
        except httpx.HTTPError:
            raise SpotifyProviderError("Spotify token endpoint unreachable") from None
        finally:
            if owns_client:
                http_client.close()

        if resp.status_code != 200:
            invalid_grant = False
            try:
                error_body = resp.json()
                invalid_grant = (
                    isinstance(error_body, dict)
                    and error_body.get("error") == "invalid_grant"
                )
            except ValueError:
                pass
            if invalid_grant:
                row.status = "error"
                db.commit()
            logger.warning("Spotify member token mint failed: status=%s", resp.status_code)
            raise SpotifyProviderError("Spotify token exchange rejected")

        try:
            body = resp.json()
        except ValueError:
            raise SpotifyProviderError("Spotify token response was not valid JSON")
        if not isinstance(body, dict):
            raise SpotifyProviderError("Spotify token response was not valid JSON")
        access_token = body.get("access_token")
        if not access_token:
            raise SpotifyProviderError("Spotify token response missing access_token")

        rotated_refresh_token = body.get("refresh_token")
        if (
            isinstance(rotated_refresh_token, str)
            and rotated_refresh_token
            and rotated_refresh_token != refresh_token
        ):
            try:
                rotated_ciphertext = self._kms_encrypt(rotated_refresh_token)
            except SpotifyConnectNotConfiguredError:
                # _kms_encrypt logs the exception type only. Keep the old payload and
                # return the successfully minted access token.
                pass
            else:
                row.payload = json.dumps(
                    {
                        "v": 1,
                        "ciphertext": rotated_ciphertext,
                        "scope": (
                            body["scope"] if "scope" in body else payload.get("scope", "")
                        ),
                        "expires_in": (
                            body["expires_in"]
                            if "expires_in" in body
                            else payload.get("expires_in", 3600)
                        ),
                        "obtained_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                db.commit()

        return {
            "access_token": str(access_token),
            "expires_in": int(body.get("expires_in", 3600)),
            "token_type": str(body.get("token_type", "Bearer")),
        }

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
    ) -> Optional["PublicNowPlaying"]:
        """Handle-scoped public read of a member's now-playing, merged across
        BOTH worker-written caches (2026-07-14 audit / RFC OQ7): the Last.fm
        now-playing row (present ⇒ playing; the poller delete+inserts it) and
        the Spotify member singleton (V45, is_playing flag). The actively
        playing source wins; when both play, the most recently written wins.

        Provenance: Last.fm connects are unverified usernames, so a Last.fm
        pick carries `source_username` for the public "via Last.fm @x" label.
        Spotify is OAuth-proven — no username exposed.

        Raises MemberNotFoundError for an unknown handle (route maps to 404);
        a member without integrations and one with nothing playing are both
        plain None — the public profile hides the section either way, and not
        distinguishing them keeps integration status private."""
        user = db.scalar(select(User).where(User.handle == handle.lower()))
        if user is None:
            raise MemberNotFoundError(handle)

        lastfm = self.lastfm_now_playing(db, user.id)
        spotify = db.get(SpotifyMemberNowPlaying, user.id)
        spotify_playing = spotify is not None and bool(spotify.is_playing)

        if lastfm is None and not spotify_playing:
            return None

        pick = "lastfm" if lastfm is not None else "spotify"
        if lastfm is not None and spotify_playing:
            # Both claim to be playing — trust the most recently written cache.
            # Missing timestamps (defensive; both columns are server-defaulted)
            # sort oldest so the dated source wins.
            floor = datetime.min.replace(tzinfo=timezone.utc)
            lastfm_at = lastfm.created_at or floor
            spotify_at = spotify.updated_at or floor  # type: ignore[union-attr]
            pick = "spotify" if spotify_at > lastfm_at else "lastfm"

        if pick == "lastfm":
            assert lastfm is not None
            username = db.scalar(
                select(UserIntegration.username).where(
                    UserIntegration.user_id == user.id,
                    UserIntegration.provider == LASTFM_PROVIDER,
                )
            )
            return PublicNowPlaying(
                source="lastfm",
                artist=lastfm.artist_name,
                track=lastfm.track_name,
                album=lastfm.album_name,
                image_url=lastfm.image_url,
                played_at=lastfm.played_at,
                source_username=username,
            )

        assert spotify is not None
        return PublicNowPlaying(
            source="spotify",
            artist=spotify.artist_name,
            track=spotify.track_name,
            album=spotify.album_name,
            image_url=spotify.image_url,
            played_at=None,
            source_username=None,
        )
