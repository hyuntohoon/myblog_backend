from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock


def _override(app, svc):
    # get_db lazily imported (module-top import pulls app.core.config early).
    from app.db.session import get_db
    from app.di import get_integration_service

    app.dependency_overrides[get_integration_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestIntegrationRoutes:
    def test_list_returns_integrations(self, client, app):
        svc = MagicMock()
        svc.list_integrations.return_value = [
            SimpleNamespace(
                provider="lastfm", username="rj", status="connected", last_synced_at=None
            )
        ]
        _override(app, svc)
        resp = client.get("/api/integrations")
        assert resp.status_code == 200
        row = resp.json()["integrations"][0]
        assert row["provider"] == "lastfm" and row["username"] == "rj"
        app.dependency_overrides.clear()

    def test_connect_lastfm_forwards_username(self, client, app):
        svc = MagicMock()
        svc.connect_lastfm.return_value = SimpleNamespace(
            provider="lastfm", username="rj", status="connected", last_synced_at=None
        )
        _override(app, svc)
        resp = client.put("/api/integrations/lastfm", json={"username": "rj"})
        assert resp.status_code == 200
        assert resp.json()["username"] == "rj"
        assert svc.connect_lastfm.call_args.args[-1] == "rj"
        app.dependency_overrides.clear()

    def test_connect_lastfm_rejects_empty(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        resp = client.put("/api/integrations/lastfm", json={"username": ""})
        assert resp.status_code == 422  # Field(min_length=1)
        app.dependency_overrides.clear()

    def test_disconnect_returns_204(self, client, app):
        svc = MagicMock()
        svc.disconnect.return_value = True
        _override(app, svc)
        resp = client.delete("/api/integrations/lastfm")
        assert resp.status_code == 204
        assert svc.disconnect.call_args.args[-1] == "lastfm"
        app.dependency_overrides.clear()

    def test_now_playing_none_is_not_playing(self, client, app):
        svc = MagicMock()
        svc.lastfm_now_playing.return_value = None
        _override(app, svc)
        resp = client.get("/api/integrations/lastfm/now-playing")
        assert resp.status_code == 200
        assert resp.json()["is_playing"] is False
        app.dependency_overrides.clear()

    def test_now_playing_returns_track(self, client, app):
        svc = MagicMock()
        svc.lastfm_now_playing.return_value = SimpleNamespace(
            artist_name="RM", track_name="들꽃놀이 (with 조유진)", album_name="Indigo",
            image_url=None, played_at=None,
        )
        _override(app, svc)
        resp = client.get("/api/integrations/lastfm/now-playing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_playing"] is True and data["track"] == "들꽃놀이 (with 조유진)"
        app.dependency_overrides.clear()


class TestIntegrationServiceUnit:
    def test_connect_creates_when_absent(self):
        from app.services.integration_service import IntegrationService
        from myblog_shared_db.models import UserIntegration

        users = MagicMock()
        users.get_or_create.return_value = SimpleNamespace(id=uuid.UUID(int=1))
        db = MagicMock()
        db.scalar.return_value = None  # no existing row
        svc = IntegrationService(users=users)
        svc.connect_lastfm(db, uuid.UUID(int=1), {}, "  rj  ")
        added = db.add.call_args.args[0]
        assert isinstance(added, UserIntegration)
        assert added.username == "rj"  # stripped
        assert added.provider == "lastfm" and added.status == "connected"
        assert db.commit.called

    def test_connect_updates_existing(self):
        from app.services.integration_service import IntegrationService

        users = MagicMock()
        users.get_or_create.return_value = SimpleNamespace(id=uuid.UUID(int=1))
        existing = SimpleNamespace(username="old", status="error")
        db = MagicMock()
        db.scalar.return_value = existing
        svc = IntegrationService(users=users)
        svc.connect_lastfm(db, uuid.UUID(int=1), {}, "new")
        assert existing.username == "new" and existing.status == "connected"
        db.add.assert_not_called()
        assert db.commit.called
