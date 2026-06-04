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
