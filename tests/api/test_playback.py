from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_integration_service, get_playback_service
from app.services.integration_service import (
    IntegrationService,
    SpotifyConnectNotConfiguredError,
    SpotifyProviderError,
)
from app.services.playback_service import (
    PlaybackItemNotFoundError,
    PlaybackNotConfiguredError,
    PlaybackProviderError,
    PlaybackService,
)

_UUID = "11111111-1111-1111-1111-111111111111"


def _override(app, svc):
    app.dependency_overrides[get_playback_service] = lambda: svc


def _override_member_path(app, svc, db, claims):
    """Route the Step-2 member path deterministically: one mock service serves both
    DI seams, get_db returns the fake session, and the verified claims are injected
    (conftest forces ENV=local, whose bypass would otherwise yield {} = owner path)."""
    _override(app, svc)
    app.dependency_overrides[get_integration_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_cognito_token] = lambda: claims


class TestSpotifyTokenRoute:
    def test_dormant_returns_503_not_configured(self, client, app):
        # Step-3 reality: no streaming refresh token provisioned → 503, never a 500/501,
        # and the route never minted anything.
        svc = MagicMock()
        svc.mint_streaming_token.side_effect = PlaybackNotConfiguredError()
        _override(app, svc)

        resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 503
        app.dependency_overrides.clear()

    def test_returns_token_when_configured(self, client, app):
        svc = MagicMock()
        svc.mint_streaming_token.return_value = {
            "access_token": "BQ-abc",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        _override(app, svc)

        resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"] == "BQ-abc"
        assert body["expires_in"] == 3600
        # owner is read from the verified JWT sub (local/dev → the single-owner sentinel).
        assert svc.mint_streaming_token.call_args.kwargs["owner"] == "owner"
        app.dependency_overrides.clear()

    def test_member_sub_uses_member_integration_token(self, client, app):
        # FEAT-member-player Step 2: a verified non-owner sub mints from the member's
        # own KMS-enveloped integration, never the owner streaming credential.
        import app.api.routes.playback as playback_route

        member_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        db, svc = MagicMock(), MagicMock()
        svc.mint_member_access_token.return_value = {
            "access_token": "member-access",
            "expires_in": 1800,
            "token_type": "Bearer",
        }
        _override_member_path(app, svc, db, {"sub": str(member_id)})

        with patch.object(
            playback_route, "settings", SimpleNamespace(OWNER_SUB="different-owner")
        ):
            resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 200
        assert resp.json()["access_token"] == "member-access"
        svc.mint_member_access_token.assert_called_once_with(db, member_id)
        svc.mint_streaming_token.assert_not_called()
        app.dependency_overrides.clear()

    def test_owner_sub_preserves_owner_token_path(self, client, app, monkeypatch):
        from app.core.config import settings

        owner_sub = "33333333-3333-3333-3333-333333333333"
        monkeypatch.setattr(settings, "OWNER_SUB", owner_sub)
        db, svc = MagicMock(), MagicMock()
        svc.mint_streaming_token.return_value = {
            "access_token": "owner-access",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        _override_member_path(app, svc, db, {"sub": owner_sub})

        resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 200
        svc.mint_streaming_token.assert_called_once_with(owner=owner_sub)
        svc.mint_member_access_token.assert_not_called()
        app.dependency_overrides.clear()

    def test_local_empty_claims_preserve_owner_sentinel_path(self, client, app):
        # ENV=local claims are {} — the pre-Step-2 behavior (owner sentinel path) must
        # survive so local dev keeps working without a Cognito token.
        db, svc = MagicMock(), MagicMock()
        svc.mint_streaming_token.return_value = {
            "access_token": "local-owner-access",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        _override_member_path(app, svc, db, {})

        resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 200
        svc.mint_streaming_token.assert_called_once_with(owner="owner")
        svc.mint_member_access_token.assert_not_called()
        app.dependency_overrides.clear()

    def test_member_not_configured_maps_to_503(self, client, app, monkeypatch):
        from app.core.config import settings

        member_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        monkeypatch.setattr(settings, "OWNER_SUB", "different-owner")
        db, svc = MagicMock(), MagicMock()
        svc.mint_member_access_token.side_effect = SpotifyConnectNotConfiguredError()
        _override_member_path(app, svc, db, {"sub": str(member_id)})

        resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 503
        assert resp.json()["detail"] == "Spotify playback not configured"
        svc.mint_streaming_token.assert_not_called()
        app.dependency_overrides.clear()

    def test_member_provider_error_maps_to_502(self, client, app, monkeypatch):
        from app.core.config import settings

        member_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        monkeypatch.setattr(settings, "OWNER_SUB", "different-owner")
        db, svc = MagicMock(), MagicMock()
        svc.mint_member_access_token.side_effect = SpotifyProviderError("rejected")
        _override_member_path(app, svc, db, {"sub": str(member_id)})

        resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 502
        svc.mint_streaming_token.assert_not_called()
        app.dependency_overrides.clear()

    def test_provider_error_maps_to_502(self, client, app):
        svc = MagicMock()
        svc.mint_streaming_token.side_effect = PlaybackProviderError("rejected")
        _override(app, svc)

        resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 502
        app.dependency_overrides.clear()

    def test_requires_jwt_in_prod(self, client, app):
        # Carried must-fix (b): the token route must be Cognito-gated. The dedicated
        # apigateway JWT route guards it at the edge (verified live), and the in-app
        # require_cognito_token is the belt-and-suspenders. This pins that in-app gate so
        # an accidental drop of the dependency can't ship green (conftest forces ENV=local,
        # which bypasses auth — flip to prod here, mirroring test_list_requires_jwt_in_prod).
        import app.core.auth as auth_module

        svc = MagicMock()
        _override(app, svc)
        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.get("/api/playback/spotify-token")

        assert resp.status_code == 401
        svc.mint_streaming_token.assert_not_called()
        app.dependency_overrides.clear()


class TestMintStreamingToken:
    def test_no_streaming_token_raises_not_configured(self, monkeypatch):
        # No creds anywhere → dormant. Critically, NO outbound call is attempted (rule #9).
        monkeypatch.setattr(PlaybackService, "_creds_cache", {"val": None, "ts": 0.0})
        svc = PlaybackService()
        monkeypatch.setattr(svc, "_read_spotify_secret", lambda: {})

        called = {"n": 0}

        class _Boom:
            def post(self, *a, **k):
                called["n"] += 1
                raise AssertionError("must not call Spotify when dormant")

        try:
            svc.mint_streaming_token(owner="owner", client=_Boom())
            assert False, "expected PlaybackNotConfiguredError"
        except PlaybackNotConfiguredError:
            pass
        assert called["n"] == 0

    def test_exchanges_refresh_token_for_access_token(self, monkeypatch):
        monkeypatch.setattr(PlaybackService, "_creds_cache", {"val": None, "ts": 0.0})
        svc = PlaybackService()
        monkeypatch.setattr(
            svc,
            "_read_spotify_secret",
            lambda: {
                "client_id": "cid",
                "client_secret": "csec",
                "streaming_refresh_token": "rt-streaming",
            },
        )

        captured = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"access_token": "BQ-xyz", "expires_in": 3600, "token_type": "Bearer"}

        class _Client:
            def post(self, url, data=None, auth=None):
                captured["url"] = url
                captured["data"] = data
                captured["auth"] = auth
                return _Resp()

        tok = svc.mint_streaming_token(owner="owner", client=_Client())

        assert tok["access_token"] == "BQ-xyz"
        assert tok["expires_in"] == 3600
        # Exchanges the dedicated streaming refresh token against the Spotify AUTH host.
        assert captured["url"] == "https://accounts.spotify.com/api/token"
        assert captured["data"]["refresh_token"] == "rt-streaming"
        assert captured["data"]["grant_type"] == "refresh_token"
        assert captured["auth"] == ("cid", "csec")

    def test_spotify_rejection_raises_provider_error(self, monkeypatch):
        monkeypatch.setattr(PlaybackService, "_creds_cache", {"val": None, "ts": 0.0})
        svc = PlaybackService()
        monkeypatch.setattr(
            svc,
            "_read_spotify_secret",
            lambda: {
                "client_id": "cid",
                "client_secret": "csec",
                "streaming_refresh_token": "rt",
            },
        )

        class _Resp:
            status_code = 400

            @staticmethod
            def json():
                return {"error": "invalid_grant"}

        class _Client:
            def post(self, *a, **k):
                return _Resp()

        try:
            svc.mint_streaming_token(owner="owner", client=_Client())
            assert False, "expected PlaybackProviderError"
        except PlaybackProviderError:
            pass

    def test_non_json_200_raises_provider_error_not_500(self, monkeypatch):
        # A 200 with a non-JSON body must surface as PlaybackProviderError (→ 502), never an
        # unhandled JSONDecodeError escaping the route as a 500.
        monkeypatch.setattr(PlaybackService, "_creds_cache", {"val": None, "ts": 0.0})
        svc = PlaybackService()
        monkeypatch.setattr(
            svc,
            "_read_spotify_secret",
            lambda: {"client_id": "cid", "client_secret": "csec", "streaming_refresh_token": "rt"},
        )

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                raise ValueError("not json")

        class _Client:
            def post(self, *a, **k):
                return _Resp()

        try:
            svc.mint_streaming_token(owner="owner", client=_Client())
            assert False, "expected PlaybackProviderError"
        except PlaybackProviderError:
            pass


class TestMintMemberAccessToken:
    """IntegrationService.mint_member_access_token — the member half of the Step-2
    route. Fake db rows + patched kms_envelope; no real DB, KMS, or Spotify call."""

    member_id = uuid.UUID("22222222-2222-2222-2222-222222222222")

    def _service(self, monkeypatch, **overrides):
        from app.core.config import settings

        values = {
            "USER_TOKENS_KMS_KEY_ID": "alias/myblog-user-tokens",
            "SPOTIFY_CLIENT_ID": "cid",
            "SPOTIFY_CLIENT_SECRET": "csec",
        }
        values.update(overrides)
        for key, value in values.items():
            monkeypatch.setattr(settings, key, value)
        return IntegrationService(users=MagicMock())

    @staticmethod
    def _row(*, status="connected", payload=None):
        if payload is None:
            payload = json.dumps(
                {
                    "v": 1,
                    "ciphertext": "old-envelope",
                    "scope": "streaming user-read-email",
                    "expires_in": 3600,
                    "obtained_at": "2026-07-01T00:00:00+00:00",
                }
            )
        return SimpleNamespace(status=status, payload=payload)

    @staticmethod
    def _http_response(body, status_code=200):
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=status_code)
        http.post.return_value.json.return_value = body
        return http

    def test_kms_key_unset_fails_before_db_or_outbound(self, monkeypatch):
        svc = self._service(monkeypatch, USER_TOKENS_KMS_KEY_ID="")
        db, http = MagicMock(), MagicMock()

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass

        db.scalar.assert_not_called()
        http.post.assert_not_called()

    def test_missing_app_creds_fail_before_db_or_outbound(self, monkeypatch):
        svc = self._service(monkeypatch, SPOTIFY_CLIENT_ID="", SPOTIFY_CLIENT_SECRET="")
        monkeypatch.setattr(svc, "_spotify_app_creds", lambda: ("", ""))
        db, http = MagicMock(), MagicMock()

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass

        db.scalar.assert_not_called()
        http.post.assert_not_called()

    def test_missing_row_is_not_configured_without_outbound(self, monkeypatch):
        svc, db, http = self._service(monkeypatch), MagicMock(), MagicMock()
        db.scalar.return_value = None

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass

        http.post.assert_not_called()

    def test_non_connected_row_is_not_configured_without_outbound(self, monkeypatch):
        svc, db, http = self._service(monkeypatch), MagicMock(), MagicMock()
        db.scalar.return_value = self._row(status="error")

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass

        http.post.assert_not_called()

    def test_payload_without_ciphertext_is_not_configured(self, monkeypatch):
        svc, db, http = self._service(monkeypatch), MagicMock(), MagicMock()
        db.scalar.return_value = self._row(payload=json.dumps({"v": 1, "scope": "streaming"}))

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass

        http.post.assert_not_called()

    def test_decrypt_failure_is_not_configured(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, http = self._service(monkeypatch), MagicMock(), MagicMock()
        db.scalar.return_value = self._row()
        monkeypatch.setattr(
            kms_envelope,
            "kms_decrypt_b64",
            MagicMock(side_effect=RuntimeError("kms unavailable")),
        )

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyConnectNotConfiguredError")
        except SpotifyConnectNotConfiguredError:
            pass

        http.post.assert_not_called()

    def test_happy_path_exchanges_member_refresh_token(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, row = self._service(monkeypatch), MagicMock(), self._row()
        db.scalar.return_value = row
        decrypt = MagicMock(return_value="member-refresh")
        monkeypatch.setattr(kms_envelope, "kms_decrypt_b64", decrypt)
        http = self._http_response(
            {"access_token": "member-access", "expires_in": 1800, "token_type": "Bearer"}
        )

        token = svc.mint_member_access_token(db, self.member_id, client=http)

        assert token == {
            "access_token": "member-access",
            "expires_in": 1800,
            "token_type": "Bearer",
        }
        decrypt.assert_called_once_with("old-envelope")
        assert http.post.call_args.args[0] == "https://accounts.spotify.com/api/token"
        assert http.post.call_args.kwargs["data"] == {
            "grant_type": "refresh_token",
            "refresh_token": "member-refresh",
        }
        assert http.post.call_args.kwargs["auth"] == ("cid", "csec")
        db.commit.assert_not_called()

    def test_rotated_refresh_token_updates_envelope_and_commits(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, row = self._service(monkeypatch), MagicMock(), self._row()
        db.scalar.return_value = row
        monkeypatch.setattr(kms_envelope, "kms_decrypt_b64", lambda _: "member-refresh")
        encrypt = MagicMock(return_value="new-envelope")
        monkeypatch.setattr(kms_envelope, "kms_encrypt_b64", encrypt)
        http = self._http_response(
            {
                "access_token": "member-access",
                "refresh_token": "rotated-refresh",
                "scope": "streaming user-read-email user-read-playback-state",
                "expires_in": 2700,
            }
        )

        token = svc.mint_member_access_token(db, self.member_id, client=http)

        assert token["access_token"] == "member-access"
        encrypt.assert_called_once_with("rotated-refresh")
        stored = json.loads(row.payload)
        assert stored["v"] == 1
        assert stored["ciphertext"] == "new-envelope"
        # scope/expires_in track the refresh response when present (worker
        # _rotated_payload twin — the response reflects the actual grant).
        assert stored["scope"] == "streaming user-read-email user-read-playback-state"
        assert stored["expires_in"] == 2700
        assert stored["obtained_at"] != "2026-07-01T00:00:00+00:00"
        db.commit.assert_called_once_with()

    def test_same_refresh_token_returned_skips_rotation(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, row = self._service(monkeypatch), MagicMock(), self._row()
        old_payload = row.payload
        db.scalar.return_value = row
        monkeypatch.setattr(kms_envelope, "kms_decrypt_b64", lambda _: "member-refresh")
        encrypt = MagicMock()
        monkeypatch.setattr(kms_envelope, "kms_encrypt_b64", encrypt)
        http = self._http_response(
            {"access_token": "member-access", "refresh_token": "member-refresh"}
        )

        token = svc.mint_member_access_token(db, self.member_id, client=http)

        assert token["access_token"] == "member-access"
        assert row.payload == old_payload
        encrypt.assert_not_called()
        db.commit.assert_not_called()

    def test_rotation_encrypt_failure_keeps_payload_and_returns_token(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, row = self._service(monkeypatch), MagicMock(), self._row()
        old_payload = row.payload
        db.scalar.return_value = row
        monkeypatch.setattr(kms_envelope, "kms_decrypt_b64", lambda _: "member-refresh")
        monkeypatch.setattr(
            kms_envelope,
            "kms_encrypt_b64",
            MagicMock(side_effect=RuntimeError("kms unavailable")),
        )
        http = self._http_response(
            {"access_token": "member-access", "refresh_token": "rotated-refresh"}
        )

        token = svc.mint_member_access_token(db, self.member_id, client=http)

        assert token["access_token"] == "member-access"
        assert row.payload == old_payload
        db.commit.assert_not_called()

    def test_invalid_grant_marks_row_error_and_raises(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, row = self._service(monkeypatch), MagicMock(), self._row()
        db.scalar.return_value = row
        monkeypatch.setattr(kms_envelope, "kms_decrypt_b64", lambda _: "member-refresh")
        http = self._http_response({"error": "invalid_grant"}, status_code=400)

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyProviderError")
        except SpotifyProviderError:
            pass

        # Revoked/rotated-away token → the row is flagged so the front can
        # surface a reconnect; the mint still fails with a clean 502.
        assert row.status == "error"
        db.commit.assert_called_once_with()

    def test_non_invalid_grant_rejection_leaves_row_untouched(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, row = self._service(monkeypatch), MagicMock(), self._row()
        db.scalar.return_value = row
        monkeypatch.setattr(kms_envelope, "kms_decrypt_b64", lambda _: "member-refresh")
        http = self._http_response({"error": "server_error"}, status_code=500)

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyProviderError")
        except SpotifyProviderError:
            pass

        assert row.status == "connected"
        db.commit.assert_not_called()

    def test_non_json_200_raises_provider_error(self, monkeypatch):
        from app.core import kms_envelope

        svc, db, row = self._service(monkeypatch), MagicMock(), self._row()
        db.scalar.return_value = row
        monkeypatch.setattr(kms_envelope, "kms_decrypt_b64", lambda _: "member-refresh")
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=200)
        http.post.return_value.json.side_effect = ValueError("not json")

        try:
            svc.mint_member_access_token(db, self.member_id, client=http)
            raise AssertionError("expected SpotifyProviderError")
        except SpotifyProviderError:
            pass


class TestResolveRoute:
    """GET /api/playback/resolve — FEAT-spotify-streaming-playback Step 2. The route reads
    the DB via get_db; tests override both the service and get_db so no real DB is needed."""

    def _override_db(self, app):
        from app.db.session import get_db
        app.dependency_overrides[get_db] = lambda: None

    def test_resolve_returns_uri(self, client, app):
        svc = MagicMock()
        svc.resolve_uri.return_value = "spotify:track:abc123"
        _override(app, svc)
        self._override_db(app)

        resp = client.get(f"/api/playback/resolve?type=track&id={_UUID}")

        assert resp.status_code == 200
        assert resp.json()["uri"] == "spotify:track:abc123"
        # query keys are type/id (RFC), threaded to the service as item_type/item_id.
        assert svc.resolve_uri.call_args.kwargs == {"item_type": "track", "item_id": _UUID}
        app.dependency_overrides.clear()

    def test_resolve_not_found_maps_to_404(self, client, app):
        svc = MagicMock()
        svc.resolve_uri.side_effect = PlaybackItemNotFoundError("album:x")
        _override(app, svc)
        self._override_db(app)

        resp = client.get(f"/api/playback/resolve?type=album&id={_UUID}")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_resolve_bad_type_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        self._override_db(app)

        resp = client.get(f"/api/playback/resolve?type=podcast&id={_UUID}")

        assert resp.status_code == 422
        svc.resolve_uri.assert_not_called()
        app.dependency_overrides.clear()

    def test_resolve_missing_params_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        self._override_db(app)

        resp = client.get("/api/playback/resolve?type=track")  # no id

        assert resp.status_code == 422
        svc.resolve_uri.assert_not_called()
        app.dependency_overrides.clear()


class TestResolveUri:
    """PlaybackService.resolve_uri unit tests with a fake db (no real DB / no Spotify call)."""

    def _db_returning(self, spotify_id):
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = spotify_id
        return db

    def test_resolve_track_uri(self):
        uri = PlaybackService().resolve_uri(self._db_returning("tk1"), item_type="track", item_id=_UUID)
        assert uri == "spotify:track:tk1"

    def test_resolve_album_uri(self):
        uri = PlaybackService().resolve_uri(self._db_returning("al1"), item_type="album", item_id=_UUID)
        assert uri == "spotify:album:al1"

    def test_missing_row_raises_not_found(self):
        try:
            PlaybackService().resolve_uri(self._db_returning(None), item_type="track", item_id=_UUID)
            assert False, "expected PlaybackItemNotFoundError"
        except PlaybackItemNotFoundError:
            pass

    def test_malformed_id_raises_without_db_query(self):
        # A non-UUID id must 404 BEFORE any DB query (a UUID column comparison would else error).
        db = MagicMock()
        try:
            PlaybackService().resolve_uri(db, item_type="track", item_id="not-a-uuid")
            assert False, "expected PlaybackItemNotFoundError"
        except PlaybackItemNotFoundError:
            pass
        db.query.assert_not_called()
