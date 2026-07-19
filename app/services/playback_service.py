# app/services/playback_service.py
"""Spotify Web Playback SDK token mint (async, auth-host only).

GET /api/playback/spotify-token exchanges a `streaming`-scope refresh token for a
short-lived access token the client SDK plays with. Since FEAT-member-player Step 2
the route is per-member: ``mint_member_streaming_token`` exchanges the caller's
KMS-enveloped row-scoped refresh token (stored by IntegrationService.connect_spotify);
``mint_streaming_token`` keeps the owner special case. rule #9 holds: the play path is
entirely client-side; the server only mints/refreshes a token against the Spotify
*auth* endpoint (accounts.spotify.com), never proxying a Spotify *content* call
(api.spotify.com) on a user-facing request.

The owner path stays DORMANT until the Step-5 `streaming` OAuth consent provisions a
streaming refresh token into the myblog/spotify secret. Until then
``mint_streaming_token`` raises ``PlaybackNotConfiguredError`` and NO outbound call
ever fires. The streaming refresh token is DISTINCT from the worker's read-only
``refresh_token`` (which carries no `streaming` scope), so it must never be derived
from that one.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import kms_envelope
from app.core.config import settings
from myblog_shared_db.models import Album, Track, UserIntegration

logger = logging.getLogger(__name__)


class PlaybackNotConfiguredError(Exception):
    """Required playback credential or KMS configuration is unavailable — the route
    maps this to 503 (fail closed, never a silent bypass)."""


class PlaybackNotConnectedError(Exception):
    """No usable connected Spotify integration exists for this member — 404."""


class PlaybackProviderError(Exception):
    """Spotify rejected the token exchange (invalid_grant / network / 5xx) — 502."""


class PlaybackGrantRevokedError(PlaybackProviderError):
    """Spotify named `invalid_grant` (refresh token revoked or rotated away) — still a
    502, but the member mint flags the integration row 'error' so the integrations tab
    can prompt a reconnect and the next mint is a clean 404."""


class PlaybackItemNotFoundError(Exception):
    """No catalog Album/Track with that id, or the row has no spotify_id, or the id is
    malformed — the resolve route maps this to 404 (FEAT-spotify-streaming-playback Step 2)."""


# accounts.spotify.com is the AUTH host (token mint), NOT the Web API content host. A mint
# here is the rule-#9-blessed exception (RFC D3) — it is not a synchronous Spotify content
# call. Kept as a constant (not a setting) since it is a fixed Spotify endpoint.
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

# The streaming creds change only when the owner re-consents (~never), so cache the secret
# read across warm Lambda invocations rather than hitting SSM on every play-token refresh.
_CREDS_TTL_SEC = 300.0


class PlaybackService:
    """Mints short-lived Spotify Web Playback SDK access tokens."""

    _creds_cache: dict = {"val": None, "ts": 0.0}

    def _read_spotify_secret(self) -> Dict:
        """The myblog/spotify secret JSON, or {} when unset/unreadable. Mirrors
        get_spotify_connection_status' on-demand read (SSM preferred → Secrets Manager)."""
        param = settings.SPOTIFY_SECRETS_PARAM
        arn = settings.SPOTIFY_SECRETS_ARN
        if not param and not arn:
            return {}
        try:
            import boto3

            if param:
                ssm = boto3.client("ssm", region_name=settings.AWS_DEFAULT_REGION)
                raw = ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
            else:
                sm = boto3.client("secretsmanager", region_name=settings.AWS_DEFAULT_REGION)
                raw = sm.get_secret_value(SecretId=arn)["SecretString"]
            return json.loads(raw)
        except Exception as e:  # pragma: no cover - IAM/network failure path
            logger.error("Failed to read Spotify secret for playback token: %s", e)
            return {}

    def resolve_uri(self, db: Session, *, item_type: str, item_id: str) -> str:
        """Map a catalog DB id → a Spotify URI via the stored ``spotify_id`` — a direct DB
        read, NO Spotify search (rule #9-safe). ``spotify:album:<id>`` is played as a
        context_uri; ``spotify:track:<id>`` as a uris[] entry. Raises
        PlaybackItemNotFoundError (→404) for a malformed/unknown id or a row with no
        spotify_id. ``item_type`` is constrained to 'album'|'track' by the route's Literal."""
        try:
            uuid.UUID(str(item_id))  # str() so a non-str caller can't raise an uncaught error
        except ValueError:
            raise PlaybackItemNotFoundError(f"{item_type}:{item_id}")
        model = Album if item_type == "album" else Track
        spotify_id = db.query(model.spotify_id).filter(model.id == item_id).scalar()
        if not spotify_id:
            raise PlaybackItemNotFoundError(f"{item_type}:{item_id}")
        return f"spotify:{item_type}:{spotify_id}"

    def _load_streaming_creds(self) -> Dict[str, str]:
        """Resolve {client_id, client_secret, streaming_refresh_token}. Settings (env)
        override the secret so local/test is deterministic; in prod the env vars are empty
        and the myblog/spotify secret supplies them. TTL-cached."""
        now = time.time()
        cached = self._creds_cache.get("val")
        if cached is not None and now - self._creds_cache["ts"] < _CREDS_TTL_SEC:
            return cached
        payload = self._read_spotify_secret()
        creds = {
            "client_id": settings.SPOTIFY_CLIENT_ID or payload.get("client_id", ""),
            "client_secret": settings.SPOTIFY_CLIENT_SECRET or payload.get("client_secret", ""),
            # A dedicated `streaming`-scope token — NEVER the worker's read-only refresh_token.
            "streaming_refresh_token": (
                settings.SPOTIFY_STREAMING_REFRESH_TOKEN
                or payload.get("streaming_refresh_token", "")
            ),
        }
        self._creds_cache.update(val=creds, ts=now)
        return creds

    def mint_streaming_token(
        self, *, owner: str, client: Optional[httpx.Client] = None
    ) -> Dict[str, object]:
        """Exchange the owner streaming refresh token → a short-lived access token.

        ``owner`` is the verified-JWT owner (threaded for the future per-owner token
        store). Raises PlaybackNotConfiguredError (503) when no streaming token/app
        creds are provisioned — the pre-Step-5 reality, before any outbound call.
        """
        creds = self._load_streaming_creds()
        if not creds["streaming_refresh_token"] or not creds["client_id"] or not creds["client_secret"]:
            # Dormant: nothing to mint. No Spotify call is made (rule #9-safe).
            raise PlaybackNotConfiguredError()

        body = self._exchange_refresh_token(
            creds["streaming_refresh_token"],
            creds["client_id"],
            creds["client_secret"],
            client,
        )
        # Spotify rotates the refresh token only occasionally; owner persistence of a
        # rotated streaming refresh token is a Step-5 concern (the consent store), so
        # the owner path does not write it back.
        return {
            "access_token": body["access_token"],
            "expires_in": int(body.get("expires_in", 3600)),
            "token_type": body.get("token_type", "Bearer"),
        }

    def _exchange_refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        client: Optional[httpx.Client] = None,
    ) -> dict:
        """Exchange one refresh token at the Spotify auth host, returning the validated
        response body. Never logs the token or secret — status codes only."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        auth = (client_id, client_secret)
        owns_client = client is None
        client = client or httpx.Client(timeout=10)
        try:
            resp = client.post(_SPOTIFY_TOKEN_URL, data=data, auth=auth)
        except httpx.HTTPError as e:
            raise PlaybackProviderError(f"Spotify token endpoint unreachable: {e}")
        finally:
            if owns_client:
                client.close()

        if resp.status_code != 200:
            # Surface a clean 502, never the token/secret; logged WITHOUT credentials.
            # invalid_grant (token revoked/rotated away) gets its own subtype so the
            # member mint can flag the row — Spotify error names are safe to inspect.
            invalid_grant = False
            try:
                err_body = resp.json()
                invalid_grant = (
                    isinstance(err_body, dict)
                    and err_body.get("error") == "invalid_grant"
                )
            except ValueError:
                pass
            logger.warning("Spotify token mint failed: status=%s", resp.status_code)
            if invalid_grant:
                raise PlaybackGrantRevokedError("Spotify token exchange rejected")
            raise PlaybackProviderError("Spotify token exchange rejected")

        try:
            body = resp.json()
        except ValueError:
            # A 200 with a non-JSON body (proxy/CDN error page) — keep it a clean 502, not
            # an unhandled JSONDecodeError escaping as a 500 (the route only catches the
            # Playback* errors).
            raise PlaybackProviderError("Spotify token response was not valid JSON")
        access_token = body.get("access_token") if isinstance(body, dict) else None
        if not access_token:
            raise PlaybackProviderError("Spotify token response missing access_token")
        return body

    def mint_member_streaming_token(
        self,
        db: Session,
        *,
        member_id: uuid.UUID,
        client: Optional[httpx.Client] = None,
    ) -> Dict[str, object]:
        """Mint from the member's row-scoped, KMS-enveloped Spotify refresh token
        (FEAT-member-player Step 2). Config gates fail closed BEFORE any DB/outbound
        work; a member without a connected integration is 404, never the owner token."""
        if not settings.USER_TOKENS_KMS_KEY_ID:
            raise PlaybackNotConfiguredError()
        creds = self._load_streaming_creds()
        if not creds["client_id"] or not creds["client_secret"]:
            raise PlaybackNotConfiguredError()

        row = db.scalar(
            select(UserIntegration).where(
                UserIntegration.user_id == member_id,
                UserIntegration.provider == "spotify",
            )
        )
        if row is None or row.status != "connected":
            raise PlaybackNotConnectedError()

        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError):
            logger.warning("Spotify member integration payload invalid")
            raise PlaybackNotConnectedError() from None
        if not isinstance(payload, dict):
            logger.warning("Spotify member integration payload invalid")
            raise PlaybackNotConnectedError()
        ciphertext = payload.get("ciphertext")
        if not isinstance(ciphertext, str) or not ciphertext:
            logger.warning("Spotify member integration ciphertext missing")
            raise PlaybackNotConnectedError()

        try:
            refresh_token = kms_envelope.kms_decrypt_b64(ciphertext)
        except Exception as e:
            logger.error("KMS decrypt for Spotify playback failed: %s", type(e).__name__)
            raise PlaybackNotConfiguredError() from None

        try:
            body = self._exchange_refresh_token(
                refresh_token,
                creds["client_id"],
                creds["client_secret"],
                client,
            )
        except PlaybackGrantRevokedError:
            # Revoked/rotated-away member token: flag the row so the integrations tab
            # can prompt a reconnect and the next mint is a clean 404 — then still 502.
            row.status = "error"
            db.commit()
            raise

        # Spotify occasionally rotates the refresh token on exchange; persist the new
        # envelope so the stored token never goes stale. A write-back failure is
        # non-fatal (never store plaintext; the old ciphertext usually stays valid).
        rotated = body.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != refresh_token:
            try:
                updated = dict(payload)
                updated["ciphertext"] = kms_envelope.kms_encrypt_b64(rotated)
                updated["obtained_at"] = datetime.now(timezone.utc).isoformat()
                row.payload = json.dumps(updated)
                db.commit()
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.error(
                    "Spotify refresh-token rotation write-back failed: %s",
                    type(e).__name__,
                )

        return {
            "access_token": body["access_token"],
            "expires_in": int(body.get("expires_in", 3600)),
            "token_type": body.get("token_type", "Bearer"),
        }
