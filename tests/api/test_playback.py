from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.di import get_playback_service
from app.services.playback_service import (
    PlaybackItemNotFoundError,
    PlaybackNotConfiguredError,
    PlaybackProviderError,
    PlaybackService,
)

_UUID = "11111111-1111-1111-1111-111111111111"


def _override(app, svc):
    app.dependency_overrides[get_playback_service] = lambda: svc


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
