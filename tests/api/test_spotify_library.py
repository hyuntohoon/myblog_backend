from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Route-level tests for FEAT-spotify-library-sync (POST sync + GET state) and the
# new `kind` field on BucketResponse. The BucketService is mocked, so no DB runs;
# config-touching modules are imported lazily inside fixtures (the `app`/`client`
# fixtures in conftest.py clear the settings cache) to avoid the collection-time
# empty-settings-singleton trap.


def _override(app, svc):
    # Import get_db lazily — a module-top import would pull app.core.config at
    # collection time and cache an empty settings singleton.
    from app.db.session import get_db
    from app.di import get_bucket_service

    app.dependency_overrides[get_bucket_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


def _prod_settings():
    s = MagicMock()
    s.ENV = "prod"
    s.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
    s.COGNITO_ALLOWED_CLIENT_IDS = "test-spa-client"
    s.COGNITO_REGION = "ap-northeast-2"
    return s


def _conn_status(connected=True, needs_reauth=False, last=None):
    from app.clients.sqs_client import SpotifyConnectionStatus

    return SpotifyConnectionStatus(
        connected=connected,
        needs_reauth=needs_reauth,
        last_successful_refresh_at=last,
    )


def _lib_row(
    album_id="alb-1",
    spotify_id="spot-1",
    source="myblog_added",
    state="synced",
    in_bucket=True,
    in_spotify=True,
    last_error=None,
):
    r = MagicMock()
    r.album_id = album_id
    r.spotify_id = spotify_id
    r.source = source
    r.state = state
    r.in_bucket = in_bucket
    r.in_spotify = in_spotify
    r.last_error = last_error
    return r


class TestSpotifyLibrarySync:
    def test_enqueues_and_returns_202_queued(self, client, app):
        from app.di import get_sqs_client

        svc = MagicMock()
        svc.library_sync_debounced.return_value = False
        _override(app, svc)
        sqs = MagicMock()
        app.dependency_overrides[get_sqs_client] = lambda: sqs

        resp = client.post("/api/buckets/spotify-library/sync")

        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"
        # get-or-create the special bucket, then enqueue exactly once.
        svc.get_or_create_spotify_library_bucket.assert_called_once()
        sqs.send_library_sync.assert_called_once()
        app.dependency_overrides.clear()

    def test_debounced_does_not_enqueue(self, client, app):
        from app.di import get_sqs_client

        svc = MagicMock()
        svc.library_sync_debounced.return_value = True
        _override(app, svc)
        sqs = MagicMock()
        app.dependency_overrides[get_sqs_client] = lambda: sqs

        resp = client.post("/api/buckets/spotify-library/sync")

        assert resp.status_code == 202
        assert resp.json()["status"] == "debounced"
        # Bucket still get-or-created, but NO message enqueued (rule #9 holds either way).
        svc.get_or_create_spotify_library_bucket.assert_called_once()
        sqs.send_library_sync.assert_not_called()
        app.dependency_overrides.clear()

    def test_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings()):
            resp = client.post("/api/buckets/spotify-library/sync")
        assert resp.status_code == 401


class TestSpotifyLibraryState:
    @staticmethod
    def _patch_conn(status):
        return patch(
            "app.api.routes.buckets.get_spotify_connection_status",
            return_value=status,
        )

    def test_returns_bucket_albums_and_flags(self, client, app):
        bucket = MagicMock()
        bucket.id = "bk-lib"
        last = datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)
        svc = MagicMock()
        svc.get_spotify_library_state.return_value = (
            bucket,
            last,
            [
                _lib_row(album_id="alb-1", source="myblog_added", state="synced"),
                _lib_row(
                    album_id="alb-2",
                    spotify_id="spot-2",
                    source="preexisting",
                    state="needs_attention",
                    in_bucket=False,
                    in_spotify=True,
                    last_error="missing scope",
                ),
            ],
        )
        _override(app, svc)

        with self._patch_conn(_conn_status(needs_reauth=True)):
            with patch(
                "app.api.routes.buckets.get_settings",
                return_value=MagicMock(SPOTIFY_LIBRARY_WRITES_ENABLED=False),
            ):
                resp = client.get("/api/buckets/spotify-library/state")

        assert resp.status_code == 200
        body = resp.json()
        assert body["bucket_id"] == "bk-lib"
        assert body["last_synced_at"].startswith("2026-06-08T10:00")
        assert body["needs_reauth"] is True
        assert body["writes_enabled"] is False
        albums = body["albums"]
        assert [a["album_id"] for a in albums] == ["alb-1", "alb-2"]
        assert albums[0]["source"] == "myblog_added"
        assert albums[0]["state"] == "synced"
        assert albums[1]["source"] == "preexisting"
        assert albums[1]["in_bucket"] is False
        assert albums[1]["last_error"] == "missing scope"
        app.dependency_overrides.clear()

    def test_empty_no_bucket_yet(self, client, app):
        svc = MagicMock()
        svc.get_spotify_library_state.return_value = (None, None, [])
        _override(app, svc)

        with self._patch_conn(_conn_status(connected=False)):
            with patch(
                "app.api.routes.buckets.get_settings",
                return_value=MagicMock(SPOTIFY_LIBRARY_WRITES_ENABLED=True),
            ):
                resp = client.get("/api/buckets/spotify-library/state")

        assert resp.status_code == 200
        body = resp.json()
        assert body["bucket_id"] is None
        assert body["last_synced_at"] is None
        assert body["needs_reauth"] is False
        # writes_enabled is the read-only mirror of the backend setting.
        assert body["writes_enabled"] is True
        assert body["albums"] == []
        app.dependency_overrides.clear()

    def test_state_requires_jwt_in_prod(self, client):
        # BUG-25 regression: this GET used to have no auth dependency at all and
        # rode the unauthenticated edge_guard catch-all. It now requires the same
        # owner-tier JWT as the sibling POST (test_requires_jwt_in_prod above) —
        # must 401 with no Authorization header in prod ENV.
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings()):
            resp = client.get("/api/buckets/spotify-library/state")
        assert resp.status_code == 401


class TestBucketKindField:
    def test_list_buckets_includes_kind(self, client, app):
        # The serializer must surface `kind` so the FRONT can filter the special
        # bucket out of the normal tree.
        root = MagicMock()
        root.id = "bk-1"
        root.name = "꼭"
        root.position = 0
        root.color = None
        root.is_done = False
        root.kind = "review"
        root.type = "general"  # FEAT-my-buckit-artist (V32): validated str on BucketResponse
        root.research_mode = "off"
        root.items = []
        root.children_nodes = []

        lib = MagicMock()
        lib.id = "bk-lib"
        lib.name = "Spotify 라이브러리"
        lib.position = 1
        lib.color = None
        lib.is_done = False
        lib.kind = "spotify_library"
        lib.type = "general"  # FEAT-my-buckit-artist (V32)
        lib.research_mode = "off"
        lib.items = []
        lib.children_nodes = []

        svc = MagicMock()
        svc.list_buckets.return_value = [root, lib]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        buckets = resp.json()["buckets"]
        kinds = {b["id"]: b["kind"] for b in buckets}
        assert kinds == {"bk-1": "review", "bk-lib": "spotify_library"}
        app.dependency_overrides.clear()
