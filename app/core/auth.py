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
# no signature, no valid claims — and on this service `edge_guard` runs it for
# every `/api/*` request on the raw invoke domain, not just guarded routes. So N
# junk requests produced N outbound fetches to Cognito's JWKS endpoint, billed
# to and rate-limited against this account, and each one made every concurrent
# legitimate request pay a fresh 10s-timeout round trip.
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

    Shared by `require_cognito_token` (FastAPI dependency) and `edge_guard`
    (middleware, STAB-2 Step 2) so both layers validate identically. A missing
    pool id raises 503 (fail closed — never a silent no-op); a JWKS-fetch outage
    raises 503; a bad/expired/malformed token raises 401.
    """
    # STAB-2 / AUTH-5: in prod a missing pool id is a MISCONFIGURATION, not a
    # reason to skip auth. Fail CLOSED — never silently fall open (the old
    # `or not COGNITO_USER_POOL_ID: return {}` made require_cognito_token a
    # no-op in prod because the backend Lambda env never set the pool id).
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
            # Cognito JWKS fetch failed (network/timeout/5xx). This is an upstream
            # availability issue, not a bad token — surface 503 instead of letting
            # the HTTPError escape as an unhandled 500. Not cached (lru_cache only
            # stores successful returns), so the next request retries.
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
        # add ID-token support has the binding already correct rather than absent. Until now nothing here looked at either, so ANY token
        # minted by this user pool was accepted — including one for an app client
        # with entirely different scopes. The API Gateway authorizer pins the SPA
        # client (infra/apigateway.tf), but it is attached to 51 of 55 routes, so
        # every backend GET and the whole music service were unbound. The live harm
        # is bounded today because only the SPA client is in use; the harm this
        # prevents is a future app client silently inheriting the entire API.
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


# FEAT-pocket-buckit Step 3 (OQ11 / OQ12): single-owner-from-sub. The drop + playback
# routes read the owner from the VERIFIED JWT `sub`, never the request body, so a later
# per-owner generalization (FEAT-multi-user-accounts) is a plain additive change. v1 has
# no `owner_id` column anywhere (OQ12 defers all multi-user scoping), so this resolves the
# *pattern* + the local/dev fallback, not a value that gets stored. `require_cognito_token`
# returns `{}` in local/dev, so `claims.get('sub')` is None there → the single-owner
# sentinel; a bare `claims['sub']` would KeyError (the carried adversarial must-fix).
SINGLE_OWNER = "owner"


def resolve_owner(claims: Dict[str, Any] | None) -> str:
    """The acting owner id: the verified JWT `sub`, else the single-owner sentinel."""
    return (claims or {}).get("sub") or SINGLE_OWNER


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


def require_owner(
    claims: Dict[str, Any] = Depends(require_cognito_token),
) -> Dict[str, Any]:
    """Owner-only gate: the verified JWT `sub` must equal `OWNER_SUB`.

    FEAT-multi-user-accounts 0c: enabling Cognito self-signup fills the pool with
    federated members, so `require_cognito_token` alone (any valid pool token) no
    longer implies the owner. Single-owner routes — editorial authoring/publish,
    genre taxonomy, and the owner's buckets/library/playback (none per-user scoped
    until Phase 2/3) — must additionally verify identity. Member-legitimate routes
    (`/api/me`, music search, `GET /api/lyrics/{id}`) keep plain
    `require_cognito_token`.

    **`/api/lyrics` left this list 2026-07-28, rejoined it 2026-08-09**
    (`CHORE-lyrics-member-guard-reopen`, owner decision 2026-08-03). The
    07-28 tightening reasoned from the Genius annotation store the route now
    also serves; the owner reverted that call. `POST
    /api/lyrics/{id}/translation-request` still uses `require_owner` — a
    separate, still-standing reason (LLM cost/quota control), not a leftover
    of this one.

    Fail closed: local/dev keeps the `require_cognito_token` bypass (claims `{}`) so
    local admin work is unblocked; in prod an unset `OWNER_SUB` is a
    misconfiguration → 503 (never fall open); a non-owner token → 403.
    """
    if settings.ENV in ("local", "dev"):
        return claims  # {} — mirrors the require_cognito_token local bypass

    if not settings.OWNER_SUB:
        logger.error(
            "OWNER_SUB unset while ENV=%s — refusing to fail open",
            settings.ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured",
        )

    if claims.get("sub") != settings.OWNER_SUB:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

    return claims


def require_owner_or_draft_agent(
    claims: Dict[str, Any] = Depends(require_cognito_token),
) -> Dict[str, Any]:
    """The owner, or the nightly draft agent — for draft creation only.

    FIX-nightly-draft-identity Phase A. `scripts/buckit_nightly.py` runs at 03:00
    with nobody logged in and must create a draft post. It used to borrow the smoke
    user, which `require_owner` has rejected since 392dd50 (2026-07-08).

    This is a SEPARATE dependency, not a widening of `require_owner`. `require_owner`
    guards 38 routes (authoring, publish, delete, genre taxonomy, the owner's
    buckets/library/playback); admitting a second `sub` there would grant the
    automation all of them to solve a problem that needs exactly one capability.
    Only `create_post` and the grow-once bucket-item PATCH use this.

    Passing this gate is not permission to publish: `create_post` COERCES a
    non-owner caller's post to `status='draft'` (it does not merely validate, so a
    future code path that forgets the check cannot open a publish hole).

    Fail closed, identically to `require_owner`: local/dev bypasses, an unset
    OWNER_SUB in prod is a misconfiguration ⇒ 503, anything else ⇒ 403. An unset
    DRAFT_AGENT_SUB means *no agent exists* and must never widen access — it is
    checked for truthiness before comparing, so `sub=None`/`""` cannot match it.
    """
    if settings.ENV in ("local", "dev"):
        return claims  # {} — mirrors the require_cognito_token local bypass

    if not settings.OWNER_SUB:
        logger.error(
            "OWNER_SUB unset while ENV=%s — refusing to fail open",
            settings.ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured",
        )

    sub = claims.get("sub")
    if sub == settings.OWNER_SUB:
        return claims
    if settings.DRAFT_AGENT_SUB and sub == settings.DRAFT_AGENT_SUB:
        return claims

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")


def is_owner(claims: Dict[str, Any]) -> bool:
    """True when these claims are the owner's.

    Used by routes that admit the draft agent to tell the two callers apart — the
    agent gets its post coerced to a draft. In local/dev `require_cognito_token`
    yields `{}`, and local admin work must keep behaving as the owner, so an empty
    claim set is treated as the owner there and only there.
    """
    if settings.ENV in ("local", "dev"):
        return True
    return bool(settings.OWNER_SUB) and claims.get("sub") == settings.OWNER_SUB
