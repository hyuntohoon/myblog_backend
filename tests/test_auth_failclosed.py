"""STAB-2 / AUTH-5: require_cognito_token must fail CLOSED in prod.

Regression lock for the fail-open bug: in prod, a missing COGNITO_USER_POOL_ID
used to return {} (no-op auth). It must now raise 503 instead of silently
opening. ENV=local/dev still bypasses (local-dev convenience).

We monkeypatch app.core.auth.settings directly (auth.py binds `settings` at
import; env-var monkeypatching wouldn't reach the cached singleton — see
reference-backend-test-config-import-collection).
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.core.auth as auth


def _settings(**kw):
    base = dict(ENV="prod", COGNITO_USER_POOL_ID="", COGNITO_REGION="ap-northeast-2")
    base.update(kw)
    return SimpleNamespace(**base)


def test_failclosed_503_when_pool_id_missing_in_prod(monkeypatch):
    monkeypatch.setattr(auth, "settings", _settings(COGNITO_USER_POOL_ID=""))
    with pytest.raises(HTTPException) as ei:
        auth.require_cognito_token(credentials=None)
    assert ei.value.status_code == 503  # NOT a silent {} fall-open


def test_local_env_still_bypasses(monkeypatch):
    monkeypatch.setattr(auth, "settings", _settings(ENV="local", COGNITO_USER_POOL_ID=""))
    assert auth.require_cognito_token(credentials=None) == {}


def test_prod_with_pool_id_requires_a_token(monkeypatch):
    # pool id present + ENV=prod => no bypass; missing credentials => 401 (not 503, not {})
    monkeypatch.setattr(auth, "settings", _settings(COGNITO_USER_POOL_ID="ap-northeast-2_test"))
    with pytest.raises(HTTPException) as ei:
        auth.require_cognito_token(credentials=None)
    assert ei.value.status_code == 401
