from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from app.di import get_library_service
from app.services.library_service import AlbumNotFoundError


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


def _library_item(album_id="alb-1", status="wishlist"):
    it = MagicMock()
    it.album_id = album_id
    it.status = status
    it.added_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    it.updated_at = datetime(2026, 6, 2, tzinfo=timezone.utc)
    it.album = _album(album_id=album_id)
    return it


def _override(app, svc):
    # Lazy get_db import: a module-top import pulls app.core.config at collection
    # time, caching an empty settings singleton (cf. test_buckets._override).
    from app.db.session import get_db

    app.dependency_overrides[get_library_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestListLibrary:
    def test_list_returns_items(self, client, app):
        svc = MagicMock()
        svc.list_items.return_value = [_library_item(status="listening")]
        _override(app, svc)

        resp = client.get("/api/library")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["album_id"] == "alb-1"
        assert item["status"] == "listening"
        assert item["album"]["title"] == "Album"
        app.dependency_overrides.clear()

    def test_list_empty(self, client, app):
        svc = MagicMock()
        svc.list_items.return_value = []
        _override(app, svc)

        resp = client.get("/api/library")

        assert resp.status_code == 200
        assert resp.json()["items"] == []
        app.dependency_overrides.clear()


class TestSetStatus:
    def test_set_returns_200_created(self, client, app):
        svc = MagicMock()
        svc.set_status.return_value = (_library_item(status="listened"), True)
        _override(app, svc)

        resp = client.put("/api/library/alb-1", json={"status": "listened"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["album_id"] == "alb-1"
        assert body["status"] == "listened"
        assert svc.set_status.call_args.kwargs == {"status": "listened"}
        app.dependency_overrides.clear()

    def test_set_returns_200_updated(self, client, app):
        svc = MagicMock()
        svc.set_status.return_value = (_library_item(status="reviewed"), False)
        _override(app, svc)

        resp = client.put("/api/library/alb-1", json={"status": "reviewed"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "reviewed"
        app.dependency_overrides.clear()

    def test_set_missing_album_returns_404(self, client, app):
        svc = MagicMock()
        svc.set_status.side_effect = AlbumNotFoundError("alb-x")
        _override(app, svc)

        resp = client.put("/api/library/alb-x", json={"status": "wishlist"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_set_bad_status_returns_422(self, client, app):
        _override(app, MagicMock())
        resp = client.put("/api/library/alb-1", json={"status": "bogus"})
        assert resp.status_code == 422
        app.dependency_overrides.clear()

    def test_set_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.put("/api/library/alb-1", json={"status": "wishlist"})

        assert resp.status_code == 401


class TestDeleteItem:
    def test_delete_returns_204(self, client, app):
        svc = MagicMock()
        svc.delete_item.return_value = True
        _override(app, svc)

        resp = client.delete("/api/library/alb-1")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_missing_returns_404(self, client, app):
        svc = MagicMock()
        svc.delete_item.return_value = False
        _override(app, svc)

        resp = client.delete("/api/library/no-such")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_delete_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.delete("/api/library/alb-1")

        assert resp.status_code == 401
