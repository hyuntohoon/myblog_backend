"""Cognito JWT verification — the canonical copy.

SEC-system-hardening Step 6. This file is **byte-identical** in
`myblog_backend/app/core/auth.py` and `myblog_music/app/core/auth.py`. Both
repositories name their application package `app` and define the same four
settings fields (`ENV`, `COGNITO_REGION`, `COGNITO_USER_POOL_ID`,
`COGNITO_ALLOWED_CLIENT_IDS`), so the shared text needs no injection seam — the
same source compiles and runs unchanged in either service.

Before Step 6 these were two files whose *code* was already token-for-token
identical; only the comments and one structural split differed (backend had
factored `verify_token` out for `edge_guard`, music had it inlined). Step 6
removes the second copy's right to drift, it does not change what either
service accepts or rejects.

**Editing rule:** a change here must land in both repositories. The workspace
repository runs a daily drift check that diffs the two `main` copies and fails
when they differ; `docs/rfcs/SEC-system-hardening.md` Step 6 records why the
enforcement is a scheduled cross-repo diff rather than a shared package (the
three services pin `myblog_shared_db` at three different revisions today, one of
them an abandoned tag, so a packaged verifier would have shipped two different
versions of this code and made an auth hotfix a pin migration).

**Scope:** authentication only — *is this a valid token from our pool, for an
app client we issue to*. Authorization tiers are deliberately NOT here, because
they differ per service: `myblog_backend` keeps owner / draft-agent / member
tiers in `app/core/authz.py`; `myblog_music` has none beyond this file.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any, Dict

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def _get_jwks() -> Dict[str, Any]:
    url = (
        f"https://cognito-idp.{settings.COGNITO_REGION}.amazonaws.com"
        f"/{settings.COGNITO_USER_POOL_ID}/.well-known/jwks.json"
    )
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


# SEC-system-hardening: bound the kid-miss refetch.
#
# An unknown `kid` used to call `_get_jwks.cache_clear()` unconditionally before
# returning 401. That path is reachable by anyone who can base64 a JWT header —
# no signature, no valid claims — and on `myblog_backend` `edge_guard` runs it
# for every `/api/*` request on the raw invoke domain, not just guarded routes.
# So N junk requests produced N outbound fetches to Cognito's JWKS endpoint,
# billed to and rate-limited against this account, and each one made every
# concurrent legitimate request pay a fresh 10s-timeout round trip.
#
# Key rotation is still picked up — a rotated key produces exactly this kid miss
# — just at a bounded rate rather than once per request.
#
# Caveat, unmeasured: `time.monotonic()` is CLOCK_MONOTONIC, which is not
# guaranteed to advance while a Lambda microVM is frozen between invocations. On a
# low-traffic container "60 seconds" of monotonic time can therefore span much more
# wall time, widening the window in which a rotated key returns 401. Verifying that
# needs a real Lambda logging monotonic() against time() across a cold gap, which
# has not been done. AWS does not auto-rotate user-pool signing keys today, so this
# is a documented unknown rather than a live risk.
_JWKS_REFRESH_MIN_INTERVAL_SECONDS = 60.0
_jwks_last_refresh = 0.0
_jwks_refreshes = 0


def _refresh_jwks_if_due() -> None:
    """Drop the cached JWKS, at most once per `_JWKS_REFRESH_MIN_INTERVAL_SECONDS`."""
    global _jwks_last_refresh, _jwks_refreshes
    now = time.monotonic()
    if _jwks_last_refresh and now - _jwks_last_refresh < _JWKS_REFRESH_MIN_INTERVAL_SECONDS:
        return
    _jwks_last_refresh = now
    _jwks_refreshes += 1
    _get_jwks.cache_clear()


def _jwks_refresh_count() -> int:
    """Refreshes performed since process start. For tests and diagnostics."""
    return _jwks_refreshes


def _reset_jwks_refresh_throttle() -> None:
    """Test seam — the throttle is process-global state."""
    global _jwks_last_refresh, _jwks_refreshes
    _jwks_last_refresh = 0.0
    _jwks_refreshes = 0


def _allowed_client_ids() -> frozenset[str]:
    """The Cognito app clients whose tokens this service accepts.

    Comma-separated in config so a client can be added or retired from Terraform
    without a code deploy.
    """
    raw = settings.COGNITO_ALLOWED_CLIENT_IDS or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def verify_token(token: str) -> Dict[str, Any]:
    """Validate a Cognito JWT string, returning its claims or raising HTTPException.

    Called by `require_cognito_token` (the FastAPI dependency) in both services,
    and additionally by `edge_guard` (middleware, STAB-2 Step 2) in
    `myblog_backend`, so both layers validate identically. A missing pool id
    raises 503 (fail closed — never a silent no-op); a JWKS-fetch outage raises
    503; a bad/expired/malformed token raises 401.
    """
    # STAB-2 / AUTH-5 / FIX-bug-audit-2026-07 WS-A: in prod a missing pool id is a
    # MISCONFIGURATION, not a reason to skip auth. Fail CLOSED — never silently
    # fall open. The removed shape was `or not COGNITO_USER_POOL_ID: return {}`,
    # which made `require_cognito_token` a no-op in prod on `myblog_backend`
    # (whose Lambda env never set the pool id) and would have silently un-gated
    # `/candidates` on `myblog_music` (sync Spotify read + SQS enqueue) if the
    # musicApi Lambda env ever dropped it.
    if not settings.COGNITO_USER_POOL_ID:
        logger.error(
            "COGNITO_USER_POOL_ID unset while ENV=%s — refusing to fail open",
            settings.ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured",
        )

    # SEC-system-hardening: the app-client binding is configuration, so an unset
    # allowlist is a misconfiguration and takes the same posture as an unset pool
    # id — 503, never "skip the check". Checked before any crypto so a bad deploy
    # is loud immediately rather than after a signature verification.
    allowed_clients = _allowed_client_ids()
    if not allowed_clients:
        logger.error(
            "COGNITO_ALLOWED_CLIENT_IDS unset while ENV=%s — refusing to fail open",
            settings.ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured",
        )

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        try:
            jwks = _get_jwks()
        except httpx.HTTPError as e:
            # STAB-2 Step 4: a Cognito JWKS fetch failure (network/timeout/5xx)
            # is an upstream availability issue, not a bad token. Surface 503
            # instead of letting the HTTPError escape as an unhandled 500. Not
            # cached (lru_cache only stores successful returns), so the next
            # request retries.
            logger.error("JWKS fetch failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth provider unavailable",
            )
        key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
        if key is None:
            _refresh_jwks_if_due()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown token key")

        issuer = (
            f"https://cognito-idp.{settings.COGNITO_REGION}.amazonaws.com"
            f"/{settings.COGNITO_USER_POOL_ID}"
        )
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_at_hash": False},
        )

        token_use = claims.get("token_use")
        if token_use not in ("access", "id"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

        # SEC-system-hardening: bind the token to an app client we actually issue
        # to. Cognito puts the client on `client_id` for access tokens and `aud`
        # for id tokens — but the `aud` half is unreachable today, because
        # `jwt.decode` is called without `audience=` and jose rejects any token
        # carrying `aud` before this line. It is written out so that whoever does
        # add ID-token support has the binding already correct rather than absent.
        # Until this landed nothing here looked at either, so ANY token minted by
        # this user pool was accepted — including one for an app client with
        # entirely different scopes. The API Gateway authorizer pins the SPA
        # client (infra/apigateway.tf), but it is attached to 51 of 55 routes and
        # to none of `/api/music/*`, so every backend GET and the whole music
        # service were unbound — on `myblog_music` this in-process check is the
        # only place the app client is bound at all. The live harm is bounded
        # today because only the SPA client is in use; the harm this prevents is a
        # future app client silently inheriting the entire API.
        presented_client = claims.get("client_id") if token_use == "access" else claims.get("aud")
        if presented_client not in allowed_clients:
            logger.warning(
                "token for app client %r rejected — not in COGNITO_ALLOWED_CLIENT_IDS",
                presented_client,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token client"
            )

        return claims

    except JWTError as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def require_cognito_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Dict[str, Any]:
    if settings.ENV in ("local", "dev"):
        return {}

    if credentials is None:
        # Fail closed on misconfiguration even when no token was sent, so a
        # missing pool id can never be masked as a plain 401 (matches verify_token).
        if not settings.COGNITO_USER_POOL_ID:
            logger.error(
                "COGNITO_USER_POOL_ID unset while ENV=%s — refusing to fail open",
                settings.ENV,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth not configured",
            )
        if not _allowed_client_ids():
            logger.error(
                "COGNITO_ALLOWED_CLIENT_IDS unset while ENV=%s — refusing to fail open",
                settings.ENV,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth not configured",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    return verify_token(credentials.credentials)
