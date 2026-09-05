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
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.orm import Session

from app.core import kms_envelope
from app.core.config import settings
from myblog_shared_db.models import (
    Album,
    ReviewBucket,
    ReviewBucketItem,
    Track,
    TrackProviderRef,
    UserIntegration,
)

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


class PlaybackMappingForbiddenError(Exception):
    """The caller has no STANDING to change this mapping (-> 403).

    FEAT-youtube-playback-provider OQ7: a member may change a mapping only for a
    track they hold in one of their own buckets. Not owner-only, and not
    "whoever created the row" — see PlaybackService._has_standing.
    """


class PlaybackVideoUnusableError(Exception):
    """videos.list says the proposed video cannot be embedded and played (-> 422).

    Raised BEFORE any write. The mapping table is global, so a video that fails
    verification must never reach it — one member's bad pick would otherwise be
    every member's dead playback.
    """


class PlaybackMappingGoneError(Exception):
    """The provider mapping EXISTS but is no longer playable (-> 410 Gone).

    FEAT-youtube-playback-provider OQ8. Distinct from PlaybackItemNotFoundError
    (-> 404, "never mapped") because the two demand different UI: 404 is a
    durable miss the front may cache for the tab, while this one drives the
    "wrong video, pick another" affordance and must never be cached durably —
    the A5 refresh job or a member re-pick can clear it at any time.
    """


class PlaybackItemNotFoundError(Exception):
    """No catalog Album/Track with that id, or the row has no spotify_id, or the id is
    malformed — the resolve route maps this to 404 (FEAT-spotify-streaming-playback Step 2)."""


# accounts.spotify.com is the AUTH host (token mint), NOT the Web API content host. A mint
# here is the rule-#9-blessed exception (RFC D3) — it is not a synchronous Spotify content
# call. Kept as a constant (not a setting) since it is a fixed Spotify endpoint.
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

# YouTube Developer Policy III.E.4.c/.d — stored API data must be deleted or
# refreshed within 30 calendar days, with no exception for a resource id obtained
# from a plain public search.list. Enforced at resolve time as well as by the
# Step-A5 sweep, because a sweep that is down is not a sweep.
_YT_RETENTION_DAYS = 30

# Twin of myblog_music/app/services/youtube_candidate_service.py. The `T`
# section is OPTIONAL because a live stream reports a bare `P0D` — measured
# against the live API 2026-09-06. Zero reads as UNKNOWN, never as a
# zero-second video.
_ISO_DURATION = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def _parse_iso8601_duration(value: Optional[str]) -> Optional[int]:
    """`PT4M13S` -> 253. None for anything unparseable or zero."""
    # isinstance(str), not truthiness: re.match raises TypeError on a truthy
    # non-string. Twin of the music parser.
    if not isinstance(value, str) or not value:
        return None
    m = _ISO_DURATION.match(value)
    if not m:
        return None
    d, h, mi, sec = (int(m.group(k) or 0) for k in ("d", "h", "m", "s"))
    return (d * 86400 + h * 3600 + mi * 60 + sec) or None

# The streaming creds change only when the owner re-consents (~never), so cache the secret
# read across warm Lambda invocations rather than hitting SSM on every play-token refresh.
_CREDS_TTL_SEC = 300.0


class PlaybackService:
    """Mints short-lived Spotify Web Playback SDK access tokens."""

    _creds_cache: dict = {"val": None, "ts": 0.0}

    def _read_spotify_secret(self) -> Dict:
        """The /myblog/spotify secret JSON, or {} when unset/unreadable. Mirrors
        get_spotify_connection_status' on-demand SSM read."""
        param = settings.SPOTIFY_SECRETS_PARAM
        if not param:
            return {}
        try:
            import boto3

            ssm = boto3.client("ssm", region_name=settings.AWS_DEFAULT_REGION)
            raw = ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
            return json.loads(raw)
        except Exception as e:  # pragma: no cover - IAM/network failure path
            logger.error("Failed to read Spotify secret for playback token: %s", e)
            return {}

    def resolve_uri(
        self,
        db: Session,
        *,
        item_type: str,
        item_id: str,
        provider: str = "spotify",
    ) -> str:
        """Map a catalog DB id → a playback URI for ``provider`` — a direct DB read, NO
        provider search on this path (rule #9-safe).

        ``provider`` DEFAULTS TO SPOTIFY and every pre-existing caller omits it, so the
        shipped behaviour is unchanged by construction: ``spotify:album:<id>`` is played as
        a context_uri, ``spotify:track:<id>`` as a uris[] entry, both read from the in-row
        ``spotify_id``. Spotify is not, and must not become, a row in track_provider_refs.

        ``provider='youtube'`` reads the Step-A1 mapping table instead and returns
        ``youtube:video:<videoId>``. It is TRACK-ONLY: YouTube has no album-context
        equivalent of a Spotify context_uri, so an album asked for on YouTube is a 404
        rather than a guess at a playlist.

        A YouTube row only resolves while it is actually playable AND still inside its
        retention window. Three conditions, each load-bearing for a different reason:

        * ``verify_state='live'`` — a mapping the Step-A5 refresh job has since marked
          'gone' or 'not_embeddable' resolves to 404, NOT to a dead videoId. Handing the
          IFrame player an id we already know is dead turns a clean "no mapping" into an
          opaque player error. 404 is what the shipped ``uris.ts`` already treats as a
          durable miss.
        * ``last_verified_at`` inside 30 days — this is the COMPLIANCE condition, and it
          is enforced at READ time on purpose. YouTube Developer Policy III.E.4.c/.d cap
          stored API data at 30 calendar days; the Step-A5 job is what deletes such rows,
          but that job does not exist yet and, once it does, can be down for a week.
          Serving a 45-day-old mapping because the sweep is broken is exactly the failure
          the policy is about, so resolve refuses it on its own rather than trusting a
          background job to have run.
        * ``embeddable IS NOT FALSE`` — a known non-embeddable video cannot play in an
          IFrame at all. NULL passes: it means videos.list has not been asked yet, which
          is not the same as "known unplayable". NOTE that in v1 this tolerance protects
          nothing — Step A3's PUT verifies with videos.list BEFORE writing, so the only
          writer always has the value. It matters only if a future writer (a Milestone-B
          import) skips that verification, and if A3 instead lands the column NOT NULL
          this can tighten to ``IS TRUE``. Tracked as an open question on the RFC.

        Raises PlaybackItemNotFoundError (→404) for a malformed/unknown id, a row with no
        spotify_id, or an absent/unplayable provider mapping. ``item_type`` is constrained
        to 'album'|'track' and ``provider`` to 'spotify'|'youtube' by the route's Literals.
        """
        try:
            uuid.UUID(str(item_id))  # str() so a non-str caller can't raise an uncaught error
        except ValueError:
            raise PlaybackItemNotFoundError(f"{provider}:{item_type}:{item_id}")

        if provider == "youtube":
            if item_type != "track":
                # No YouTube analogue of an album context_uri — see the docstring.
                raise PlaybackItemNotFoundError(f"youtube:{item_type}:{item_id}")
            # ONE round trip that answers both questions: does a row exist, and
            # is it still playable. The playability predicate is evaluated in
            # SQL rather than in Python so the RETENTION CLOCK IS THE DATABASE'S
            # — comparing `last_verified_at` against a Python `now()` would let
            # a skewed Lambda clock serve API data past the III.E.4 ceiling.
            row = db.execute(
                select(
                    TrackProviderRef.external_id,
                    and_(
                        TrackProviderRef.verify_state == "live",
                        # The III.E.4 retention window, enforced at READ time so a
                        # stalled refresh job cannot make us serve expired API data.
                        TrackProviderRef.last_verified_at
                        > func.now() - text("interval '%d days'" % _YT_RETENTION_DAYS),
                        # V56 (OQ9): IS TRUE, not IS NOT FALSE. `embeddable` is now
                        # NOT NULL, so the NULL tolerance the A1 version carried has
                        # no value left to admit — and IS NOT FALSE on a NOT NULL
                        # column reads as if it might.
                        TrackProviderRef.embeddable.is_(True),
                    ).label("playable"),
                ).where(
                    TrackProviderRef.track_id == item_id,
                    TrackProviderRef.provider == "youtube",
                )
            ).first()
            if row is None:
                # No mapping was ever made. The caller may offer to create one.
                raise PlaybackItemNotFoundError(f"youtube:track:{item_id}")
            if not row.playable:
                # OQ8: the row EXISTS but is dead, expired, or no longer
                # embeddable. This is a different answer from "never mapped" and
                # the front-end must be able to tell them apart: a 404 is a
                # durable miss it may cache for the tab, while this state drives
                # the "wrong video, pick another" affordance and must never be
                # cached durably — the A5 job or a re-pick can clear it.
                raise PlaybackMappingGoneError(f"youtube:track:{item_id}")
            return f"youtube:video:{row.external_id}"

        model = Album if item_type == "album" else Track
        spotify_id = db.query(model.spotify_id).filter(model.id == item_id).scalar()
        if not spotify_id:
            raise PlaybackItemNotFoundError(f"{item_type}:{item_id}")
        return f"spotify:{item_type}:{spotify_id}"

    # ------------------------------------------------------------------
    # FEAT-youtube-playback-provider Step A3 — mapping writes.
    # ------------------------------------------------------------------

    def _has_standing(self, db: Session, *, member_id: uuid.UUID, track_id: str) -> bool:
        """Does this member hold the track in one of their OWN buckets? (OQ7)

        This is the authorization predicate for both mutations, and it is
        STANDING rather than ownership on purpose. `track_provider_refs` is
        GLOBAL — one row per track for everyone — so "who created the row" is
        not a meaningful permission: the row affects every member equally.

        `require_owner` was the obvious alternative and was rejected on a
        MEASUREMENT, not on taste: the 29 live `item_type='playback'` rows split
        owner 16 / one non-owner member 13, so an owner-only gate would lock the
        second-heaviest user of this exact surface out of fixing their own bad
        matches.

        `created_by_member_id` exists on the row and is NEVER read here. Reading
        it would reintroduce per-member ownership through the back door, which
        is precisely what OQ7 decided against.

        No `item_type` filter, deliberately: a track in the member's system
        playback queue counts, because pressing play is exactly how a member
        discovers that a mapping is wrong. Restricting to 'track' rows would
        deny standing to the person best placed to notice the defect.

        Known and accepted limitation: a track NOBODY has bucketed cannot be
        mapped. An album-view picker must bucket first.
        """
        return db.execute(
            select(ReviewBucketItem.id)
            .join(ReviewBucket, ReviewBucket.id == ReviewBucketItem.bucket_id)
            .where(
                ReviewBucketItem.track_id == track_id,
                ReviewBucket.user_id == member_id,
            )
            .limit(1)
        ).first() is not None

    def set_youtube_mapping(
        self,
        db: Session,
        *,
        member_id: uuid.UUID,
        track_id: str,
        video_id: str,
    ) -> Dict:
        """Verify a videoId with the API, then upsert the global mapping.

        THE VERIFICATION IS NOT OPTIONAL AND IS NOT THE CLIENT'S TO SUPPLY.
        Because the row is global, accepting `embeddable` / `privacy_status`
        from the request body would let one member write a mapping every other
        member resolves and fails to play. The four status fields come from
        `videos.list` on the server or the write does not happen.

        Session lifecycle: the outbound call happens BEFORE any write and the
        read that precedes it is committed first, so no transaction is held
        across the network call (the recurring bug class that produced the Neon
        ProtocolViolation).
        """
        try:
            uuid.UUID(str(track_id))
        except ValueError:
            raise PlaybackItemNotFoundError(f"youtube:track:{track_id}")

        exists = db.execute(
            select(Track.id).where(Track.id == track_id).limit(1)
        ).first()
        if exists is None:
            raise PlaybackItemNotFoundError(f"youtube:track:{track_id}")
        if not self._has_standing(db, member_id=member_id, track_id=track_id):
            raise PlaybackMappingForbiddenError(track_id)

        # Release the transaction before the outbound call.
        db.commit()

        from app.clients.youtube_client import youtube

        details = youtube.list_videos([video_id])
        item = details.get(video_id)
        if item is None:
            # videos.list reports a deleted/private/nonexistent id by OMISSION.
            # Refusing here is what keeps a dead id out of the global table.
            raise PlaybackVideoUnusableError(f"{video_id}: not found, deleted or private")
        status = item.get("status") or {}
        if status.get("embeddable") is not True:
            raise PlaybackVideoUnusableError(f"{video_id}: embedding disabled by the owner")
        if status.get("privacyStatus") != "public":
            raise PlaybackVideoUnusableError(f"{video_id}: not public")

        duration_sec = _parse_iso8601_duration((item.get("contentDetails") or {}).get("duration"))

        row = db.execute(
            select(TrackProviderRef).where(
                TrackProviderRef.track_id == track_id,
                TrackProviderRef.provider == "youtube",
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if row is None:
            row = TrackProviderRef(
                track_id=track_id,
                provider="youtube",
                external_kind="video",
                source="user_confirmed",
                created_by_member_id=member_id,
            )
            db.add(row)
        # created_by_member_id is NOT reassigned on a re-point: it records who
        # FIRST confirmed this mapping. It is audit, not ownership, so rewriting
        # it on every edit would destroy the only thing it is for.
        row.external_id = video_id
        row.embeddable = True
        row.privacy_status = status.get("privacyStatus")
        row.made_for_kids = status.get("madeForKids")
        row.duration_sec = duration_sec
        row.verify_state = "live"
        row.last_verified_at = now
        row.updated_at = now
        db.commit()
        return {
            "track_id": str(track_id),
            "provider": "youtube",
            "video_id": video_id,
            "duration_sec": duration_sec,
            "verified_at": now,
        }

    def delete_youtube_mapping(
        self, db: Session, *, member_id: uuid.UUID, track_id: str
    ) -> None:
        """The "wrong video" action. Same standing check as the write.

        Idempotent: deleting a mapping that is not there is a 204, not a 404.
        The caller's intent — "this track must not resolve to that video" — is
        satisfied either way, and a 404 here would make the UI report a failure
        for a state the member asked for.
        """
        try:
            uuid.UUID(str(track_id))
        except ValueError:
            raise PlaybackItemNotFoundError(f"youtube:track:{track_id}")
        if not self._has_standing(db, member_id=member_id, track_id=track_id):
            raise PlaybackMappingForbiddenError(track_id)
        db.execute(
            delete(TrackProviderRef).where(
                TrackProviderRef.track_id == track_id,
                TrackProviderRef.provider == "youtube",
            )
        )
        db.commit()

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
