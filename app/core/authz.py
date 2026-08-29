"""Authorization tiers for myblog_backend — owner, draft agent, member.

SEC-system-hardening Step 6 split this out of `app/core/auth.py`. That file is
now the canonical Cognito **authentication** verifier, byte-identical with
`myblog_music/app/core/auth.py` and held that way by the workspace drift check.
These tiers are `myblog_backend`-only — music has no owner concept — so keeping
them in the shared file would have made byte-identity impossible.

Nothing about the tiers themselves changed in that split: the functions below
are the previous definitions verbatim.

Settings are read as `auth.settings.*`, not through a local
`from app.core.config import settings` binding, and that is deliberate. Every
existing test seam overrides the tiers by monkeypatching `app.core.auth.settings`
(see `tests/test_draft_agent_identity.py`, `tests/api/test_todays_pick.py` and a
dozen others). Rebinding the name in a second module would have silently
detached those overrides from the tier checks; going through `auth` keeps one
binding, so one patch still governs both layers exactly as it did before.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, status

from app.core import auth
# Private alias: these tiers depend on the member guard, but `authz` must not
# become a second import path for it. One spelling, one module — the route
# inventory greps a name, not a source module.
from app.core.auth import require_cognito_token as _require_cognito_token

logger = logging.getLogger(__name__)


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


def require_owner(
    claims: Dict[str, Any] = Depends(_require_cognito_token),
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
    if auth.settings.ENV in ("local", "dev"):
        return claims  # {} — mirrors the require_cognito_token local bypass

    if not auth.settings.OWNER_SUB:
        logger.error(
            "OWNER_SUB unset while ENV=%s — refusing to fail open",
            auth.settings.ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured",
        )

    if claims.get("sub") != auth.settings.OWNER_SUB:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

    return claims


def require_owner_or_draft_agent(
    claims: Dict[str, Any] = Depends(_require_cognito_token),
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
    if auth.settings.ENV in ("local", "dev"):
        return claims  # {} — mirrors the require_cognito_token local bypass

    if not auth.settings.OWNER_SUB:
        logger.error(
            "OWNER_SUB unset while ENV=%s — refusing to fail open",
            auth.settings.ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured",
        )

    sub = claims.get("sub")
    if sub == auth.settings.OWNER_SUB:
        return claims
    if auth.settings.DRAFT_AGENT_SUB and sub == auth.settings.DRAFT_AGENT_SUB:
        return claims

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")


def is_owner(claims: Dict[str, Any]) -> bool:
    """True when these claims are the owner's.

    Used by routes that admit the draft agent to tell the two callers apart — the
    agent gets its post coerced to a draft. In local/dev `require_cognito_token`
    yields `{}`, and local admin work must keep behaving as the owner, so an empty
    claim set is treated as the owner there and only there.
    """
    if auth.settings.ENV in ("local", "dev"):
        return True
    return bool(auth.settings.OWNER_SUB) and claims.get("sub") == auth.settings.OWNER_SUB
