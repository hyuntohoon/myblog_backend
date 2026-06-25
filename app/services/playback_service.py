# app/services/playback_service.py
"""FEAT-pocket-buckit Step 3 (D3 / OQ8) — async Spotify Web Playback SDK token mint.

GET /api/playback/spotify-token exchanges the owner's per-listener `streaming`-scope
refresh token for a short-lived access token the client SDK plays with. rule #9 holds:
the play path is entirely client-side; the server only mints/refreshes a token against
the Spotify *auth* endpoint (accounts.spotify.com), never proxying a Spotify *content*
call (api.spotify.com) on a user-facing request.

DORMANT until the Step-5 `streaming` OAuth consent provisions a streaming refresh token
into the myblog/spotify secret. Until then ``mint_streaming_token`` raises
``PlaybackNotConfiguredError`` and NO outbound call ever fires — so the deployed Step-3
endpoint is rule-#9-safe by construction (there is nothing to call yet). The streaming
refresh token is DISTINCT from the worker's read-only ``refresh_token`` (which carries no
`streaming` scope), so it must never be derived from that one.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Dict, Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from myblog_shared_db.models import Album, Track

logger = logging.getLogger(__name__)


class PlaybackNotConfiguredError(Exception):
    """No `streaming`-scope refresh token (or app creds) is provisioned yet — the route
    maps this to 503. Expected in Step 3 (the consent is wired in Step 5)."""


class PlaybackProviderError(Exception):
    """Spotify rejected the token exchange (invalid_grant / network / 5xx) — 502."""


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
    """Mints short-lived Spotify Web Playback SDK access tokens (single-owner, v1)."""

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
        """Exchange the streaming refresh token → a short-lived access token.

        ``owner`` is the verified-JWT owner (single-owner v1; threaded for the future
        per-owner token store). Raises PlaybackNotConfiguredError (503) when no streaming
        token/app creds are provisioned — the Step-3 reality, before any outbound call.
        """
        creds = self._load_streaming_creds()
        if not creds["streaming_refresh_token"] or not creds["client_id"] or not creds["client_secret"]:
            # Dormant: nothing to mint. No Spotify call is made (rule #9-safe).
            raise PlaybackNotConfiguredError()

        data = {
            "grant_type": "refresh_token",
            "refresh_token": creds["streaming_refresh_token"],
        }
        auth = (creds["client_id"], creds["client_secret"])
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
            # Body may name invalid_grant (token revoked) etc. — surface a clean 502, never
            # the token/secret. Logged WITHOUT the credentials.
            logger.warning("Spotify token mint failed: status=%s", resp.status_code)
            raise PlaybackProviderError("Spotify token exchange rejected")

        try:
            body = resp.json()
        except ValueError:
            # A 200 with a non-JSON body (proxy/CDN error page) — keep it a clean 502, not
            # an unhandled JSONDecodeError escaping as a 500 (the route only catches the two
            # Playback* errors).
            raise PlaybackProviderError("Spotify token response was not valid JSON")
        access_token = body.get("access_token")
        if not access_token:
            raise PlaybackProviderError("Spotify token response missing access_token")
        # Spotify rotates the refresh token only occasionally; v1 single-owner persistence
        # of a rotated streaming refresh token is a Step-5 concern (the consent store), so
        # we do not write it back here.
        return {
            "access_token": access_token,
            "expires_in": int(body.get("expires_in", 3600)),
            "token_type": body.get("token_type", "Bearer"),
        }
