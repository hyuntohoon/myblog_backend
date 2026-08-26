"""Signed-token vectors for `verify_token` — the surface no test reached before.

The 40 auth tests that existed on 2026-08-26 all avoided constructing a JWT:
they passed `credentials=None`, a literal `"x.y.z"`, or monkeypatched the
verifier away. So every property below — signature, issuer, `token_use`,
expiry, `nbf`, the RS256 pin, the app-client binding — was unverified, and a
refactor that turned any of them into a no-op would have gone unnoticed. That
is not hypothetical: `token_use in ("access", "id")` advertises ID-token
support that has never worked, because `jwt.decode` is called without
`audience=` and jose rejects any token carrying `aud`. `test_id_token_*` below
pins that so the dead branch cannot be "fixed" into a real hole by someone
passing `audience=` and concluding it works because access tokens still pass.

This module is duplicated byte-for-byte in `myblog_music` (minus the tiers
music does not have). The guards are duplicated on purpose; their vectors must
be too, or the copies drift where the duplication exists to protect.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

import app.core.auth as auth

from . import cognito_tokens as tok

# Captured before any monkeypatching so the timeout assertion below reads the
# real fetcher and not a stub.
_REAL_GET_JWKS = auth._get_jwks


def _stub_jwks(fn):
    """A `_get_jwks` stand-in carrying the `cache_clear` attribute the real
    `lru_cache`-wrapped function has, so the guard's refetch path is exercised
    rather than blowing up on a missing attribute."""
    fn.cache_clear = lambda: None
    return fn


def _settings(**kw):
    base = dict(
        ENV="prod",
        COGNITO_USER_POOL_ID=tok.POOL_ID,
        COGNITO_REGION=tok.REGION,
        COGNITO_ALLOWED_CLIENT_IDS=tok.CLIENT_ID,
        OWNER_SUB="",
        DRAFT_AGENT_SUB="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def prod_guard(monkeypatch):
    """ENV=prod with a JWKS holding exactly the fixture's public key."""
    monkeypatch.setattr(auth, "settings", _settings())
    monkeypatch.setattr(auth, "_get_jwks", _stub_jwks(lambda: tok.jwks()))
    # The kid-miss refetch throttle is process-global; reset it per test.
    if hasattr(auth, "_reset_jwks_refresh_throttle"):
        auth._reset_jwks_refresh_throttle()
    yield


def _status(token: str) -> int:
    """The status `verify_token` raises, or 200 when it returns claims."""
    try:
        auth.verify_token(token)
    except HTTPException as exc:
        return exc.status_code
    return 200


# --- the happy path, which nothing asserted before -------------------------

def test_valid_access_token_is_accepted():
    claims = auth.verify_token(tok.mint())
    assert claims["sub"] == "user-sub-1"
    assert claims["token_use"] == "access"


# --- signature and algorithm ----------------------------------------------

def test_token_signed_by_an_unknown_key_is_rejected():
    # Right kid, right claims, wrong private key: the signature must not verify.
    assert _status(tok.mint(signing_tag="attacker")) == 401


def test_alg_none_is_rejected():
    assert _status(tok.mint_unsigned()) == 401


def test_hs256_signed_with_the_rsa_modulus_is_rejected():
    # Algorithm confusion. Passes only if the verifier trusts the token's own
    # `alg` instead of pinning RS256.
    assert _status(tok.mint_hs256_with_modulus()) == 401


def test_unknown_kid_is_rejected():
    assert _status(tok.mint(kid="not-in-the-jwks")) == 401


def test_missing_kid_is_rejected():
    assert _status(tok.mint(kid=None)) == 401


# --- issuer ---------------------------------------------------------------

def test_token_from_a_different_user_pool_is_rejected():
    other = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_otherpool"
    assert _status(tok.mint(issuer=other)) == 401


def test_token_with_no_issuer_claim_is_rejected():
    assert _status(tok.mint(omit=["iss"])) == 401


# --- expiry and time claims -----------------------------------------------

def test_expired_token_is_rejected():
    assert _status(tok.mint(expires_in=-1)) == 401


def test_token_not_yet_valid_is_rejected():
    assert _status(tok.mint(not_before=3600)) == 401


def test_no_clock_skew_is_granted():
    # Documents the current posture rather than asking for one: jose's leeway is
    # 0 and neither guard overrides it, so a token 2s past exp is already dead.
    # If someone adds leeway, this test says so out loud.
    assert _status(tok.mint(expires_in=-2)) == 401


# --- token_use ------------------------------------------------------------

def test_refresh_token_use_is_rejected():
    assert _status(tok.mint(token_use="refresh")) == 401


def test_missing_token_use_is_rejected():
    assert _status(tok.mint(token_use=None)) == 401


def test_id_token_is_rejected_because_aud_is_never_supplied():
    # An ID token carries `aud`, and `jwt.decode` is called with no `audience=`,
    # so jose rejects it before `token_use` is ever read. Pinned deliberately:
    # the guard must not start accepting ID tokens as a side effect of someone
    # adding `audience=` — access tokens carry no `aud` and would keep passing,
    # so such a change would look correct in production while binding nothing.
    assert _status(tok.mint(token_use="id", extra={"aud": tok.CLIENT_ID})) == 401


# --- app-client binding ---------------------------------------------------

def test_token_from_an_unlisted_app_client_is_rejected():
    # The pool has two app clients. Only the SPA client is used by the SPA,
    # scripts/smoke.py and scripts/buckit_nightly.py, and only it is in the API
    # Gateway authorizer's audience. Without this check, a token minted for any
    # other client in the pool — including one added years from now with
    # different scopes — is accepted by every route the authorizer does not
    # cover, which is all of myblog_music and every backend GET.
    assert _status(tok.mint(client_id=tok.OTHER_CLIENT_ID)) == 401


def test_token_with_no_client_id_is_rejected():
    assert _status(tok.mint(client_id=None, omit=["client_id"])) == 401


def test_allowlist_accepts_any_of_several_configured_clients(monkeypatch):
    monkeypatch.setattr(
        auth,
        "settings",
        _settings(COGNITO_ALLOWED_CLIENT_IDS=f"{tok.OTHER_CLIENT_ID},{tok.CLIENT_ID}"),
    )
    assert auth.verify_token(tok.mint())["sub"] == "user-sub-1"


def test_unset_allowlist_fails_closed_in_prod(monkeypatch):
    # Same posture as an unset COGNITO_USER_POOL_ID: a missing binding is a
    # misconfiguration, never a reason to skip the check.
    monkeypatch.setattr(auth, "settings", _settings(COGNITO_ALLOWED_CLIENT_IDS=""))
    assert _status(tok.mint()) == 503


# --- malformed input ------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b.c", "Bearer x", "..."])
def test_malformed_tokens_are_rejected(bad):
    assert _status(bad) == 401


# --- JWKS availability ----------------------------------------------------

def test_jwks_outage_is_503_not_401(monkeypatch):
    def boom():
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(auth, "_get_jwks", boom)
    # 503, not 401: the token may be perfectly good and we simply cannot check
    # it. Answering 401 would tell every logged-in SPA to drop its session, and
    # a Cognito blip would become a synchronised mass logout plus a refresh
    # stampede back onto the same degraded endpoint.
    assert _status(tok.mint()) == 503


def test_jwks_fetch_has_an_explicit_timeout():
    # CLAUDE.md: every outbound HTTP request carries an explicit timeout. The
    # real _get_jwks is stubbed out in every other test here, so without this
    # assertion a future edit dropping `timeout=` would be invisible.
    import inspect

    src = inspect.getsource(_REAL_GET_JWKS)
    assert "timeout=" in src


def test_kid_miss_does_not_refetch_the_jwks_on_every_request(monkeypatch):
    # An unknown `kid` used to clear the cache before returning 401, and the
    # header is attacker-controlled and needs no valid signature — so N junk
    # requests produced N outbound fetches to Cognito, from the owner's own
    # account. On the backend this is reachable for every /api/* path through
    # edge_guard, not just the guarded routes. Key rotation still resolves,
    # just at a bounded rate.
    fetches = {"n": 0}

    def counting_jwks():
        fetches["n"] += 1
        return tok.jwks()

    monkeypatch.setattr(auth, "_get_jwks", _stub_jwks(counting_jwks))

    for _ in range(20):
        assert _status(tok.mint(kid="rogue-kid")) == 401

    assert auth._jwks_refresh_count() <= 1, (
        f"{auth._jwks_refresh_count()} JWKS refreshes from 20 unauthenticated requests"
    )
