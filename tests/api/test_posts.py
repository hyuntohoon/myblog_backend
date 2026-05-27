from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from app.di import get_post_service


def _make_post(post_id="uuid-1", slug="test-post", title="Test Post", status="published"):
    p = MagicMock()
    p.id = post_id
    p.slug = slug
    p.title = title
    p.description = "desc"
    p.status = status
    p.posted_date = date(2026, 5, 27)
    p.rating = 4.0
    return p


class TestListPosts:
    def test_list_all_returns_200(self, client, app):
        mock_svc = MagicMock()
        mock_svc.list.return_value = [_make_post()]
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["posts"]) == 1
        assert data["posts"][0]["slug"] == "test-post"

        app.dependency_overrides.clear()

    def test_list_filters_by_status(self, client, app):
        mock_svc = MagicMock()
        mock_svc.list.return_value = [_make_post(status="draft")]
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts?status=draft")

        assert resp.status_code == 200
        mock_svc.list.assert_called_once()
        _, kwargs = mock_svc.list.call_args
        assert kwargs.get("status") == "draft"

        app.dependency_overrides.clear()

    def test_list_empty_returns_empty_array(self, client, app):
        mock_svc = MagicMock()
        mock_svc.list.return_value = []
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts")

        assert resp.status_code == 200
        assert resp.json()["posts"] == []

        app.dependency_overrides.clear()


class TestDeletePost:
    def test_delete_existing_post_returns_204(self, client, app):
        mock_svc = MagicMock()
        mock_svc.delete.return_value = True
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.delete("/api/posts/uuid-1")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_nonexistent_post_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.delete.return_value = False
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.delete("/api/posts/no-such-id")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestUpdatePost:
    def test_update_existing_post_returns_200(self, client, app):
        mock_svc = MagicMock()
        mock_svc.update.return_value = _make_post(slug="updated-post")
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.put("/api/posts/uuid-1", json={"title": "Updated Title"})

        assert resp.status_code == 200
        assert resp.json()["slug"] == "updated-post"
        app.dependency_overrides.clear()

    def test_update_nonexistent_post_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.update.return_value = None
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.put("/api/posts/no-such-id", json={"title": "X"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_update_rating_out_of_range_returns_422(self, client, app):
        app.dependency_overrides.clear()
        resp = client.put("/api/posts/uuid-1", json={"rating": 10.0})
        assert resp.status_code == 422
