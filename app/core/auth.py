from __future__ import annotations

import logging
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
            _get_jwks.cache_clear()
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

        if claims.get("token_use") not in ("access", "id"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

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
    (`/api/me`, `/api/lyrics`, music search) keep plain `require_cognito_token`.

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
