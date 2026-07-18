from __future__ import annotations

import base64
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


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
        assert row["scope"] is None
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


class TestSpotifyConnectRoutes:
    """FEAT-multi-user 3b-c — PUT/DELETE /api/integrations/spotify."""

    def test_connect_requires_jwt_in_prod(self, client, app):
        # conftest forces ENV=local (auth bypass) — flip to prod, mirroring
        # test_requires_jwt_in_prod in test_playback.py.
        import app.core.auth as auth_module

        svc = MagicMock()
        _override(app, svc)
        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"
        with patch.object(auth_module, "settings", fake_settings):
            resp = client.put("/api/integrations/spotify", json={"code": "abc"})
        assert resp.status_code == 401
        svc.connect_spotify.assert_not_called()
        app.dependency_overrides.clear()

    def test_connect_happy_path_never_serializes_token_material(self, client, app):
        svc = MagicMock()
        svc.connect_spotify.return_value = SimpleNamespace(
            provider="spotify",
            username=None,
            status="connected",
            last_synced_at=None,
            payload='{"v":1,"ciphertext":"S3VCRT"}',  # must NOT leak
        )
        _override(app, svc)
        resp = client.put("/api/integrations/spotify", json={"code": "AQD..x"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "spotify" and data["status"] == "connected"
        assert "payload" not in data
        assert "ciphertext" not in resp.text and "S3VCRT" not in resp.text
        assert svc.connect_spotify.call_args.args[-1] == "AQD..x"
        app.dependency_overrides.clear()

    def test_connect_rejects_empty_code(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        resp = client.put("/api/integrations/spotify", json={"code": ""})
        assert resp.status_code == 422  # Field(min_length=1)
        svc.connect_spotify.assert_not_called()
        app.dependency_overrides.clear()

    def test_connect_not_configured_maps_503(self, client, app):
        from app.services.integration_service import SpotifyConnectNotConfiguredError

        svc = MagicMock()
        svc.connect_spotify.side_effect = SpotifyConnectNotConfiguredError()
        _override(app, svc)
        resp = client.put("/api/integrations/spotify", json={"code": "abc"})
        assert resp.status_code == 503
        app.dependency_overrides.clear()

    def test_connect_rejected_code_maps_400(self, client, app):
        from app.services.integration_service import SpotifyCodeRejectedError

        svc = MagicMock()
        svc.connect_spotify.side_effect = SpotifyCodeRejectedError()
        _override(app, svc)
        resp = client.put("/api/integrations/spotify", json={"code": "expired"})
        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_connect_provider_error_maps_502(self, client, app):
        from app.services.integration_service import SpotifyProviderError

        svc = MagicMock()
        svc.connect_spotify.side_effect = SpotifyProviderError("unreachable")
        _override(app, svc)
        resp = client.put("/api/integrations/spotify", json={"code": "abc"})
        assert resp.status_code == 502
        app.dependency_overrides.clear()

    def test_disconnect_returns_204(self, client, app):
        svc = MagicMock()
        svc.disconnect.return_value = True
        _override(app, svc)
        resp = client.delete("/api/integrations/spotify")
        assert resp.status_code == 204
        assert svc.disconnect.call_args.args[-1] == "spotify"
        app.dependency_overrides.clear()

    def test_list_never_exposes_payload(self, client, app):
        svc = MagicMock()
        svc.list_integrations.return_value = [
            SimpleNamespace(
                provider="spotify",
                username=None,
                status="connected",
                last_synced_at=None,
                payload=(
                    '{"v":1,"ciphertext":"S3VCRT","scope":'
                    '"user-read-currently-playing user-read-recently-played"}'
                ),
            )
        ]
        _override(app, svc)
        resp = client.get("/api/integrations")
        assert resp.status_code == 200
        row = resp.json()["integrations"][0]
        assert row["provider"] == "spotify" and row["status"] == "connected"
        assert row["scope"] == (
            "user-read-currently-playing user-read-recently-played"
        )
        assert "payload" not in row
        assert "ciphertext" not in resp.text and "S3VCRT" not in resp.text
        app.dependency_overrides.clear()

    def test_list_spotify_malformed_payload_has_no_scope(self, client, app):
        svc = MagicMock()
        svc.list_integrations.return_value = [
            SimpleNamespace(
                provider="spotify",
                username=None,
                status="connected",
                last_synced_at=None,
                payload="not-json",
            )
        ]
        _override(app, svc)
        resp = client.get("/api/integrations")
        assert resp.status_code == 200
        assert resp.json()["integrations"][0]["scope"] is None
        app.dependency_overrides.clear()


class TestConnectSpotifyServiceUnit:
    """connect_spotify: fail-closed matrix + KMS custody (mocked Spotify + KMS)."""

    def _svc(self):
        from app.services.integration_service import IntegrationService

        users = MagicMock()
        users.get_or_create.return_value = SimpleNamespace(id=uuid.UUID(int=1))
        return IntegrationService(users=users)

    def _settings(self, monkeypatch, **overrides):
        from app.core.config import settings

        base = {
            "USER_TOKENS_KMS_KEY_ID": "alias/myblog-user-tokens",
            "SPOTIFY_CLIENT_ID": "cid",
            "SPOTIFY_CLIENT_SECRET": "csec",
        }
        base.update(overrides)
        for k, v in base.items():
            monkeypatch.setattr(settings, k, v)
        return settings

    def test_kms_key_unset_fails_closed_before_any_call(self, monkeypatch):
        from app.services.integration_service import SpotifyConnectNotConfiguredError

        self._settings(monkeypatch, USER_TOKENS_KMS_KEY_ID="")
        svc, db, http = self._svc(), MagicMock(), MagicMock()
        try:
            svc.connect_spotify(db, uuid.UUID(int=1), {}, "code", client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass
        http.post.assert_not_called()  # the one-time code is not burned
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_missing_app_creds_fails_closed(self, monkeypatch):
        from app.services.integration_service import SpotifyConnectNotConfiguredError

        self._settings(monkeypatch, SPOTIFY_CLIENT_ID="", SPOTIFY_CLIENT_SECRET="")
        svc = self._svc()
        monkeypatch.setattr(svc, "_spotify_app_creds", lambda: ("", ""))
        db, http = MagicMock(), MagicMock()
        try:
            svc.connect_spotify(db, uuid.UUID(int=1), {}, "code", client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass
        http.post.assert_not_called()
        db.add.assert_not_called()

    def test_happy_path_stores_kms_ciphertext_only(self, monkeypatch):
        from myblog_shared_db.models import UserIntegration

        settings = self._settings(monkeypatch)
        svc, db = self._svc(), MagicMock()
        db.scalar.return_value = None  # no existing row → insert
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=200)
        http.post.return_value.json.return_value = {
            "access_token": "ACCESS_SECRET",
            "refresh_token": "REFRESH_SECRET",
            "scope": "user-read-recently-played user-read-currently-playing",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        blob = b"\x01\x02kms-envelope-bytes"
        with patch("boto3.client") as bc:
            bc.return_value.encrypt.return_value = {"CiphertextBlob": blob}
            row = svc.connect_spotify(db, uuid.UUID(int=1), {}, "AQDcode", client=http)

        # Exchange used the authorization-code grant + the registered redirect_uri.
        sent = http.post.call_args.kwargs.get("data") or http.post.call_args.args[1]
        assert sent["grant_type"] == "authorization_code"
        assert sent["code"] == "AQDcode"
        assert sent["redirect_uri"] == settings.SPOTIFY_MEMBER_REDIRECT_URI
        assert http.post.call_args.kwargs["auth"] == ("cid", "csec")
        # KMS got the plaintext refresh token, keyed by the configured alias.
        enc_kwargs = bc.return_value.encrypt.call_args.kwargs
        assert enc_kwargs["KeyId"] == "alias/myblog-user-tokens"
        assert enc_kwargs["Plaintext"] == b"REFRESH_SECRET"
        # The stored row: base64 ciphertext + plaintext metadata, NO raw token.
        added = db.add.call_args.args[0]
        assert isinstance(added, UserIntegration)
        assert added.provider == "spotify" and added.status == "connected"
        stored = json.loads(added.payload)
        assert stored["ciphertext"] == base64.b64encode(blob).decode("ascii")
        assert stored["scope"].startswith("user-read-")
        assert stored["expires_in"] == 3600
        assert "REFRESH_SECRET" not in added.payload
        assert "ACCESS_SECRET" not in added.payload
        assert db.commit.called
        assert row is added

    def test_happy_path_updates_existing_row(self, monkeypatch):
        self._settings(monkeypatch)
        svc, db = self._svc(), MagicMock()
        existing = SimpleNamespace(payload="old", status="reauth")
        db.scalar.return_value = existing
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=200)
        http.post.return_value.json.return_value = {"refresh_token": "R2", "scope": ""}
        with patch("boto3.client") as bc:
            bc.return_value.encrypt.return_value = {"CiphertextBlob": b"c2"}
            svc.connect_spotify(db, uuid.UUID(int=1), {}, "code2", client=http)
        assert existing.status == "connected"
        assert "R2" not in existing.payload
        assert json.loads(existing.payload)["ciphertext"] == base64.b64encode(
            b"c2"
        ).decode("ascii")
        db.add.assert_not_called()
        assert db.commit.called

    def test_spotify_4xx_raises_rejected_and_writes_nothing(self, monkeypatch):
        from app.services.integration_service import SpotifyCodeRejectedError

        self._settings(monkeypatch)
        svc, db = self._svc(), MagicMock()
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=400)
        with patch("boto3.client") as bc:
            try:
                svc.connect_spotify(db, uuid.UUID(int=1), {}, "expired", client=http)
                raise AssertionError("expected SpotifyCodeRejectedError")
            except SpotifyCodeRejectedError:
                pass
            bc.assert_not_called()  # no KMS touch on a failed exchange
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_missing_refresh_token_raises_provider_error(self, monkeypatch):
        from app.services.integration_service import SpotifyProviderError

        self._settings(monkeypatch)
        svc, db = self._svc(), MagicMock()
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=200)
        http.post.return_value.json.return_value = {"access_token": "only"}
        try:
            svc.connect_spotify(db, uuid.UUID(int=1), {}, "code", client=http)
            raise AssertionError("expected SpotifyProviderError")
        except SpotifyProviderError:
            pass
        db.add.assert_not_called()

    def test_kms_failure_fails_closed_no_plaintext_row(self, monkeypatch):
        # The CMK is not applied yet (3b-a pending) → boto3 raises → 503-shaped
        # error, and CRITICALLY no row is written (plaintext is never stored).
        from app.services.integration_service import SpotifyConnectNotConfiguredError

        self._settings(monkeypatch)
        svc, db = self._svc(), MagicMock()
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=200)
        http.post.return_value.json.return_value = {"refresh_token": "R", "scope": ""}
        with patch("boto3.client") as bc:
            bc.return_value.encrypt.side_effect = Exception("NotFoundException")
            try:
                svc.connect_spotify(db, uuid.UUID(int=1), {}, "code", client=http)
                raise AssertionError("expected SpotifyConnectNotConfiguredError")
            except SpotifyConnectNotConfiguredError:
                pass
        db.add.assert_not_called()
        db.commit.assert_not_called()


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
