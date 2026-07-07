"""FEAT-multi-user-accounts 0d — /api/me: lazy-provisioning GET, PATCH edits,
DELETE with Cognito-first ordering + owner guard. Service is DI-overridden per
the suite convention; UserService logic itself is unit-tested in
tests/test_user_service.py."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_user_service
from app.services.user_service import LOCAL_DEV_USER_ID, HandleTakenError

SUB = "6f1b2f6e-6b1a-4c3e-9a2e-2b7c8d9e0f11"


def _user(member_id=None, handle="listener-1", email="a@b.c"):
    u = MagicMock()
    u.id = member_id or uuid.UUID(SUB)
    u.email = email
    u.handle = handle
    u.display_name = "Listener One"
    u.avatar_url = None
    u.created_at = datetime(2026, 7, 7, tzinfo=timezone.utc)
    return u


def _wire(app, svc, claims=None):
    app.dependency_overrides[get_user_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()
    if claims is not None:
        app.dependency_overrides[require_cognito_token] = lambda: claims


def test_get_me_lazy_creates_and_serializes(app, client):
    svc = MagicMock()
    svc.get_or_create.return_value = _user()
    _wire(app, svc, claims={"sub": SUB, "email": "a@b.c"})

    res = client.get("/api/me")

    assert res.status_code == 200
    body = res.json()
    assert body["id"] == SUB
    assert body["handle"] == "listener-1"
    assert svc.get_or_create.call_args[0][1] == uuid.UUID(SUB)


def test_get_me_local_dev_uses_zero_uuid(app, client):
    # local env: require_cognito_token returns {} → the all-zeros member id.
    svc = MagicMock()
    svc.get_or_create.return_value = _user(member_id=LOCAL_DEV_USER_ID)
    _wire(app, svc)

    res = client.get("/api/me")

    assert res.status_code == 200
    assert svc.get_or_create.call_args[0][1] == LOCAL_DEV_USER_ID


def test_get_me_non_uuid_sub_is_401(app, client):
    svc = MagicMock()
    _wire(app, svc, claims={"sub": "not-a-uuid"})

    assert client.get("/api/me").status_code == 401
    svc.get_or_create.assert_not_called()


def test_patch_me_passes_only_sent_fields(app, client):
    svc = MagicMock()
    svc.update_me.return_value = _user(handle="new-handle")
    _wire(app, svc, claims={"sub": SUB})

    res = client.patch("/api/me", json={"handle": "new-handle"})

    assert res.status_code == 200
    assert svc.update_me.call_args.kwargs == {"handle": "new-handle"}


def test_patch_me_invalid_handle_422s_before_service(app, client):
    svc = MagicMock()
    _wire(app, svc, claims={"sub": SUB})

    # Uppercase + too short both violate ck_users_handle_format's mirror.
    assert client.patch("/api/me", json={"handle": "AB"}).status_code == 422
    svc.update_me.assert_not_called()


def test_patch_me_handle_conflict_is_409(app, client):
    svc = MagicMock()
    svc.update_me.side_effect = HandleTakenError("taken")
    _wire(app, svc, claims={"sub": SUB})

    assert client.patch("/api/me", json={"handle": "taken"}).status_code == 409


def test_delete_me_cognito_then_row(app, client):
    svc = MagicMock()
    _wire(app, svc, claims={"sub": SUB})

    with patch("app.api.routes.me.delete_cognito_user") as cognito:
        res = client.delete("/api/me")

    assert res.status_code == 204
    cognito.assert_called_once_with(SUB)
    svc.delete_me.assert_called_once()


def test_delete_me_cognito_failure_is_502_and_keeps_row(app, client):
    from app.clients.cognito_client import CognitoDeleteError

    svc = MagicMock()
    _wire(app, svc, claims={"sub": SUB})

    with patch(
        "app.api.routes.me.delete_cognito_user", side_effect=CognitoDeleteError("boom")
    ):
        res = client.delete("/api/me")

    assert res.status_code == 502
    svc.delete_me.assert_not_called()  # DB row intact → client retry converges


def test_delete_me_owner_sub_is_403(app, client, monkeypatch):
    import app.core.config as cfg

    monkeypatch.setenv("OWNER_SUB", SUB)
    cfg.get_settings.cache_clear()

    svc = MagicMock()
    _wire(app, svc, claims={"sub": SUB})

    with patch("app.api.routes.me.delete_cognito_user") as cognito:
        res = client.delete("/api/me")

    assert res.status_code == 403
    cognito.assert_not_called()
    svc.delete_me.assert_not_called()
