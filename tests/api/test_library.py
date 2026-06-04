from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from app.di import get_library_service
from app.services.library_service import (
    AlbumNotFoundError,
    DuplicateItemError,
    ItemNotFoundError,
)


def _album(album_id="alb-1", title="Album", artists=("Artist A",)):
    a = MagicMock()
    a.id = album_id
    a.title = title
    a.cover_url = "https://cdn/cover.jpg"
    a.release_date = date(2026, 5, 1)
    a.popularity = 70
    a.artists = [MagicMock() for _ in artists]
    for m, n in zip(a.artists, artists):
        m.name = n
    return a


def _to_listen_item(item_id="it-1", album_id="alb-1", position=0, note=None):
    it = MagicMock()
    it.id = item_id
    it.album_id = album_id
    it.position = position
    it.note = note
    it.added_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    it.album = _album(album_id=album_id)
    return it


def _override(app, svc):
    from app.db.session import get_db

    app.dependency_overrides[get_library_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


def _prod_settings():
    s = MagicMock()
    s.ENV = "prod"
    s.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
    s.COGNITO_REGION = "ap-northeast-2"
    return s


class TestListToListen:
    def test_list_returns_items(self, client, app):
        svc = MagicMock()
        svc.list_to_listen.return_value = [_to_listen_item(note="must hear")]
        _override(app, svc)

        resp = client.get("/api/library/to-listen")

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["album_id"] == "alb-1"
        assert items[0]["note"] == "must hear"
        assert items[0]["album"]["title"] == "Album"
        app.dependency_overrides.clear()


class TestAddToListen:
    def test_add_returns_201(self, client, app):
        svc = MagicMock()
        svc.add_to_listen.return_value = _to_listen_item()
        _override(app, svc)

        resp = client.post("/api/library/to-listen", json={"album_id": "alb-1"})

        assert resp.status_code == 201
        assert resp.json()["album_id"] == "alb-1"
        assert svc.add_to_listen.call_args.kwargs == {"album_id": "alb-1", "note": None}
        app.dependency_overrides.clear()

    def test_add_missing_album_returns_404(self, client, app):
        svc = MagicMock()
        svc.add_to_listen.side_effect = AlbumNotFoundError("alb-x")
        _override(app, svc)

        resp = client.post("/api/library/to-listen", json={"album_id": "alb-x"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_add_duplicate_returns_409(self, client, app):
        svc = MagicMock()
        svc.add_to_listen.side_effect = DuplicateItemError("alb-1")
        _override(app, svc)

        resp = client.post("/api/library/to-listen", json={"album_id": "alb-1"})

        assert resp.status_code == 409
        app.dependency_overrides.clear()

    def test_missing_album_id_returns_422(self, client, app):
        _override(app, MagicMock())
        resp = client.post("/api/library/to-listen", json={})
        assert resp.status_code == 422
        app.dependency_overrides.clear()

    def test_add_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings()):
            resp = client.post("/api/library/to-listen", json={"album_id": "alb-1"})
        assert resp.status_code == 401


class TestDeleteToListen:
    def test_delete_returns_204(self, client, app):
        svc = MagicMock()
        svc.delete_to_listen.return_value = True
        _override(app, svc)

        resp = client.delete("/api/library/to-listen/it-1")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_missing_returns_404(self, client, app):
        svc = MagicMock()
        svc.delete_to_listen.return_value = False
        _override(app, svc)

        resp = client.delete("/api/library/to-listen/no-such")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_delete_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings()):
            resp = client.delete("/api/library/to-listen/it-1")
        assert resp.status_code == 401


class TestReorderToListen:
    def test_reorder_returns_204(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        resp = client.put(
            "/api/library/to-listen/reorder",
            json={"item_ids": ["it-2", "it-1"]},
        )

        assert resp.status_code == 204
        assert svc.reorder_to_listen.call_args.args[1] == ["it-2", "it-1"]
        app.dependency_overrides.clear()

    def test_reorder_unknown_item_returns_404(self, client, app):
        svc = MagicMock()
        svc.reorder_to_listen.side_effect = ItemNotFoundError("it-x")
        _override(app, svc)

        resp = client.put(
            "/api/library/to-listen/reorder", json={"item_ids": ["it-x"]}
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_reorder_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings()):
            resp = client.put(
                "/api/library/to-listen/reorder", json={"item_ids": []}
            )
        assert resp.status_code == 401


class TestReviewed:
    def test_reviewed_groups_by_album(self, client, app):
        svc = MagicMock()
        svc.list_reviewed.return_value = [
            (_album(album_id="alb-1"), ["post-1", "post-2"])
        ]
        _override(app, svc)

        resp = client.get("/api/library/reviewed?group_by=album")

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["album_id"] == "alb-1"
        assert items[0]["review_ids"] == ["post-1", "post-2"]
        assert items[0]["album"]["title"] == "Album"
        app.dependency_overrides.clear()

    def test_reviewed_empty(self, client, app):
        svc = MagicMock()
        svc.list_reviewed.return_value = []
        _override(app, svc)

        resp = client.get("/api/library/reviewed")

        assert resp.status_code == 200
        assert resp.json()["items"] == []
        app.dependency_overrides.clear()


# ── FEAT-member-dashboard Step 3: 최근 들은 앨범 + now-playing + refresh ──────────────

class TestRecentlyListened:
    def test_lists_albums_with_played_at(self, client, app):
        svc = MagicMock()
        svc.list_recently_listened.return_value = [
            (_album(album_id="alb-1"), datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc))
        ]
        svc.last_recent_synced_at.return_value = datetime(2026, 6, 4, 10, 5, tzinfo=timezone.utc)
        _override(app, svc)

        resp = client.get("/api/library/recently-listened")

        assert resp.status_code == 200
        body = resp.json()
        items = body["items"]
        assert len(items) == 1
        assert items[0]["album_id"] == "alb-1"
        assert items[0]["last_played_at"].startswith("2026-06-04T10:00")
        assert items[0]["album"]["title"] == "Album"
        # D31: response carries last_synced_at for the UI poll.
        assert body["last_synced_at"].startswith("2026-06-04T10:05")
        app.dependency_overrides.clear()

    def test_empty(self, client, app):
        svc = MagicMock()
        svc.list_recently_listened.return_value = []
        svc.last_recent_synced_at.return_value = None
        _override(app, svc)
        resp = client.get("/api/library/recently-listened")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        # D31: empty cache → no sync timestamp to report.
        assert body["last_synced_at"] is None
        app.dependency_overrides.clear()


class TestNowPlaying:
    def test_active(self, client, app):
        svc = MagicMock()
        np = MagicMock()
        np.is_playing = True
        np.track_name = "Airbag"
        np.artist_name = "Radiohead"
        np.album_name = "OK Computer"
        np.album_id = "alb-1"
        np.progress_ms = 42000
        np.duration_ms = 284000
        np.updated_at = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
        svc.get_now_playing.return_value = np
        _override(app, svc)

        resp = client.get("/api/library/now-playing")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_playing"] is True
        assert body["track"] == "Airbag"
        assert body["album_id"] == "alb-1"
        # D28: the row carries progress/duration, but the response must not leak
        # them (fine-grained activity + frozen progress bar).
        assert "progress_ms" not in body
        assert "duration_ms" not in body
        app.dependency_overrides.clear()

    def test_idle_with_snapshot_carries_updated_at(self, client, app):
        # D28: idle response still carries updated_at so the UI shows
        # "동기화 N분 전" rather than asserting liveness.
        svc = MagicMock()
        np = MagicMock()
        np.is_playing = False
        np.updated_at = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
        svc.get_now_playing.return_value = np
        _override(app, svc)
        resp = client.get("/api/library/now-playing")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_playing"] is False
        assert body["updated_at"].startswith("2026-06-04T10:00")
        app.dependency_overrides.clear()

    def test_nothing_playing(self, client, app):
        svc = MagicMock()
        svc.get_now_playing.return_value = None
        _override(app, svc)
        resp = client.get("/api/library/now-playing")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_playing"] is False
        # No snapshot exists yet → no timestamp to report.
        assert body["updated_at"] is None
        app.dependency_overrides.clear()


class TestSpotifyConnection:
    @staticmethod
    def _patch(status):
        from app.clients.sqs_client import SpotifyConnectionStatus  # noqa: F401
        return patch(
            "app.api.routes.library.get_spotify_connection_status",
            return_value=status,
        )

    def test_connected(self, client, app):
        from app.clients.sqs_client import SpotifyConnectionStatus

        ts = datetime(2026, 6, 4, 7, 21, tzinfo=timezone.utc)
        with self._patch(SpotifyConnectionStatus(connected=True, last_successful_refresh_at=ts)):
            resp = client.get("/api/library/spotify-connection")
        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is True
        assert body["needs_reauth"] is False
        assert body["last_successful_refresh_at"] is not None

    def test_needs_reauth(self, client, app):
        # token present but the worker's last refresh hit invalid_grant → "재인증 필요"
        from app.clients.sqs_client import SpotifyConnectionStatus

        with self._patch(SpotifyConnectionStatus(connected=True, needs_reauth=True)):
            resp = client.get("/api/library/spotify-connection")
        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is True
        assert body["needs_reauth"] is True

    def test_not_connected(self, client, app):
        from app.clients.sqs_client import SpotifyConnectionStatus

        with self._patch(SpotifyConnectionStatus(connected=False)):
            resp = client.get("/api/library/spotify-connection")
        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is False
        assert body["needs_reauth"] is False
        assert body["last_successful_refresh_at"] is None


class TestRefreshRecent:
    def test_enqueues_and_returns_202(self, client, app):
        from app.di import get_sqs_client

        sqs = MagicMock()
        app.dependency_overrides[get_sqs_client] = lambda: sqs

        resp = client.post("/api/library/refresh-recent")

        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"
        sqs.send_listening_refresh.assert_called_once()
        app.dependency_overrides.clear()

    def test_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings()):
            resp = client.post("/api/library/refresh-recent")
        assert resp.status_code == 401
