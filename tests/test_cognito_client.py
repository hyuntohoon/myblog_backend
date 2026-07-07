"""FEAT-multi-user-accounts 0d — cognito_client guard rails: local/dev no-op and
the prod fail-closed on a missing pool id (matching verify_token's posture).
The happy AWS path is exercised in prod smoke, not unit-mocked here."""
from __future__ import annotations

import pytest


def test_local_env_is_noop():
    from app.clients.cognito_client import delete_cognito_user

    # conftest pins ENV=local → returns False without ever importing boto3.
    assert delete_cognito_user("6f1b2f6e-6b1a-4c3e-9a2e-2b7c8d9e0f11") is False


def test_prod_without_pool_id_fails_closed(monkeypatch):
    import app.core.config as cfg
    from app.clients.cognito_client import CognitoDeleteError, delete_cognito_user

    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    cfg.get_settings.cache_clear()
    try:
        with pytest.raises(CognitoDeleteError):
            delete_cognito_user("6f1b2f6e-6b1a-4c3e-9a2e-2b7c8d9e0f11")
    finally:
        cfg.get_settings.cache_clear()  # don't leak prod settings into later tests
