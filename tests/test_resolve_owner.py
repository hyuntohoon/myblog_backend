"""FEAT-pocket-buckit Step 3 — resolve_owner: owner from the verified JWT sub, with the
single-owner fallback. The carried adversarial must-fix is that local/dev returns {} from
require_cognito_token, so a bare claims['sub'] would KeyError — resolve_owner uses .get()."""


def test_resolve_owner_reads_sub():
    # Lazy import (memory: a config-module import at test-module top caches empty settings).
    from app.core.authz import resolve_owner

    assert resolve_owner({"sub": "cognito-uuid-123"}) == "cognito-uuid-123"


def test_resolve_owner_empty_claims_falls_back_to_sentinel():
    from app.core.authz import SINGLE_OWNER, resolve_owner

    # local/dev: require_cognito_token returns {} → the single-owner sentinel, no KeyError.
    assert resolve_owner({}) == SINGLE_OWNER


def test_resolve_owner_none_claims_falls_back():
    from app.core.authz import SINGLE_OWNER, resolve_owner

    assert resolve_owner(None) == SINGLE_OWNER
