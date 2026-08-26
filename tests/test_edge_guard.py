"""STAB-2 Step 2 / AUTH-3 + P8-7: edge_guard must validate the Bearer JWT.

Before this change `edge_guard` trusted the literal ``Bearer `` *prefix*, so a
garbage ``Bearer x`` bypassed the guard on every authorizer-less route, and the
no-auth reject path raised inside the middleware and surfaced as a **500**.

After: a request passes the guard only with (a) a matching ``x-origin-verify``
(the CloudFront edge path — every front request, incl. the public metrics
beacon and category list, reaches the backend this way) or (b) a Bearer token
that actually validates. A bad/forged/missing token gets a clean **403**; a
JWKS outage stays **503**.

Lazy imports inside fixtures/tests only — importing ``app.main`` /
``app.core.config`` at module top would cache an empty settings singleton at
collection time and break content_sync tests in other files
(reference-backend-test-config-import-collection).
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _raise(exc):
    def _f(*_a, **_k):
        raise exc
    return _f


@pytest.fixture
def prod_main(monkeypatch):
    """app.main with edge_guard active (APP_ENV=prod) and a known EDGE_SECRET."""
    import app.core.config as cfg
    cfg.get_settings.cache_clear()
    import app.main as main
    monkeypatch.setattr(main, "APP_ENV", "prod")
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            ALLOW_PUBLIC_HEALTH=True,
            EDGE_SECRET="edge-s3cret",
            FRONT_ORIGIN="https://www.ratemymusic.blog",
            ENV="prod",
        ),
    )
    yield main
    main.app.dependency_overrides.clear()
    cfg.get_settings.cache_clear()


def _client(main):
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_no_auth_returns_clean_403_not_500(prod_main):
    # The old reject path raised HTTPException inside the middleware -> 500.
    with _client(prod_main) as c:
        r = c.get("/api/posts?include_archived=true")
    assert r.status_code == 403


def test_garbage_bearer_no_longer_bypasses(prod_main, monkeypatch):
    # AUTH-3: `Bearer x` used to pass edge_guard on the prefix alone (-> route).
    monkeypatch.setattr(
        prod_main, "verify_token", _raise(HTTPException(status_code=401, detail="Invalid token"))
    )
    with _client(prod_main) as c:
        r = c.get("/api/posts?include_archived=true", headers={"Authorization": "Bearer x"})
    # SEC-system-hardening changed this from 403 to 401. What matters here is
    # unchanged — the request does NOT reach the route — but a token that was
    # presented and rejected is now reported as a credential problem instead of
    # being flattened into 403. The flattening made an expired session look
    # identical to no session, and the SPA only refreshes on 401.
    # test_no_auth_returns_clean_403_not_500 above still pins 403 for the
    # no-credential case, which is the distinction that was lost.
    assert r.status_code == 401


def test_jwks_outage_surfaces_503_not_403(prod_main, monkeypatch):
    monkeypatch.setattr(
        prod_main,
        "verify_token",
        _raise(HTTPException(status_code=503, detail="Auth provider unavailable")),
    )
    with _client(prod_main) as c:
        r = c.get("/api/posts", headers={"Authorization": "Bearer sometoken"})
    assert r.status_code == 503


def test_valid_bearer_passes_edge_guard(prod_main, monkeypatch):
    # A real (validating) JWT with no edge secret reaches the route — the direct
    # API Gateway path (e.g. the smoke client) keeps working.
    from app.db.session import get_db
    monkeypatch.setattr(prod_main, "verify_token", lambda _t: {"sub": "u1", "token_use": "access"})
    prod_main.app.dependency_overrides[get_db] = lambda: None
    with _client(prod_main) as c:
        r = c.get("/api/db/ping", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200


def test_valid_edge_secret_passes_without_bearer(prod_main):
    # CloudFront path: public metrics beacon / category list carry x-origin-verify
    # and no Bearer — must still pass.
    from app.db.session import get_db
    prod_main.app.dependency_overrides[get_db] = lambda: None
    with _client(prod_main) as c:
        r = c.get("/api/db/ping", headers={"x-origin-verify": "edge-s3cret"})
    assert r.status_code == 200


def test_empty_edge_secret_does_not_fail_open(prod_main, monkeypatch):
    # FIX-bug-audit-2026-07 WS-A: if the secrets load failed, EDGE_SECRET is "".
    # A request carrying an empty x-origin-verify header used to compare equal
    # ("" == "") and pass — fail OPEN. It must now be rejected (403) with no
    # valid Bearer, matching the fail-closed posture of the Cognito path.
    monkeypatch.setattr(prod_main.settings, "EDGE_SECRET", "")
    with _client(prod_main) as c:
        r = c.get("/api/posts?include_archived=true", headers={"x-origin-verify": ""})
    assert r.status_code == 403


# --- verify_token unit (shared validator) ---

def _auth_settings(**kw):
    base = dict(
        ENV="prod",
        COGNITO_USER_POOL_ID="ap-northeast-2_test",
        COGNITO_REGION="ap-northeast-2",
        # SEC-system-hardening: an unset app-client allowlist fails closed (503),
        # so a prod-shaped settings object must pin it as infra/lambda.tf does.
        COGNITO_ALLOWED_CLIENT_IDS="test-spa-client",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_verify_token_503_when_pool_missing(monkeypatch):
    import app.core.auth as auth
    monkeypatch.setattr(auth, "settings", _auth_settings(COGNITO_USER_POOL_ID=""))
    with pytest.raises(HTTPException) as ei:
        auth.verify_token("any.token.here")
    assert ei.value.status_code == 503  # fail closed, never a no-op


def test_verify_token_401_on_malformed(monkeypatch):
    import app.core.auth as auth
    monkeypatch.setattr(auth, "settings", _auth_settings())
    with pytest.raises(HTTPException) as ei:
        auth.verify_token("not-a-jwt")  # JWTError before any JWKS network call
    assert ei.value.status_code == 401
