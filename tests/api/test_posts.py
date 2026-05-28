from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from app.di import get_post_service
from app.services.post_service import DuplicateSlugError


def _make_post(post_id="uuid-1", slug="test-post", title="Test Post", status="published"):
    p = MagicMock()
    p.id = post_id
    p.slug = slug
    p.title = title
    p.description = "desc"
    p.status = status
    p.posted_date = date(2026, 5, 27)
    p.rating = 4.0
    p.category = MagicMock()
    p.category.name = "music"
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
    def test_delete_default_archives_returns_200_with_status(self, client, app):
        # Default DELETE is a soft archive: service returns the updated Post,
        # route echoes id + new status so the UI can immediately reflect it.
        mock_svc = MagicMock()
        mock_svc.delete.return_value = _make_post(status="archived")
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.delete("/api/posts/uuid-1")

        assert resp.status_code == 200
        assert resp.json() == {"id": "uuid-1", "status": "archived"}
        kwargs = mock_svc.delete.call_args.kwargs
        assert kwargs.get("hard") is False
        app.dependency_overrides.clear()

    def test_delete_archive_nonexistent_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.delete.return_value = None
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.delete("/api/posts/no-such-id")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_delete_hard_true_returns_204(self, client, app):
        mock_svc = MagicMock()
        mock_svc.delete.return_value = True
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.delete("/api/posts/uuid-1?hard=true")

        assert resp.status_code == 204
        kwargs = mock_svc.delete.call_args.kwargs
        assert kwargs.get("hard") is True
        app.dependency_overrides.clear()

    def test_delete_hard_nonexistent_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.delete.return_value = False
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.delete("/api/posts/no-such-id?hard=true")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestRestorePost:
    def test_restore_existing_archived_returns_200(self, client, app):
        mock_svc = MagicMock()
        mock_svc.restore.return_value = _make_post(status="published")
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.patch("/api/posts/uuid-1/restore")

        assert resp.status_code == 200
        assert resp.json() == {"id": "uuid-1", "status": "published"}
        app.dependency_overrides.clear()

    def test_restore_nonexistent_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.restore.return_value = None
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.patch("/api/posts/no-such-id/restore")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestListPostsIncludeArchived:
    def test_include_archived_flag_forwarded(self, client, app):
        mock_svc = MagicMock()
        mock_svc.list.return_value = [_make_post(status="archived")]
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts?include_archived=true")

        assert resp.status_code == 200
        kwargs = mock_svc.list.call_args.kwargs
        assert kwargs.get("include_archived") is True
        app.dependency_overrides.clear()

    def test_include_archived_default_false(self, client, app):
        mock_svc = MagicMock()
        mock_svc.list.return_value = []
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts")

        assert resp.status_code == 200
        kwargs = mock_svc.list.call_args.kwargs
        assert kwargs.get("include_archived") is False
        app.dependency_overrides.clear()


class TestCreatePost:
    _payload = {
        "title": "Test Post",
        "body_mdx": "some body text long enough to index",
        "posted_date": "2026-05-28",
        "status": "published",
    }

    def test_create_returns_200_with_id_and_slug(self, client, app):
        mock_svc = MagicMock()
        mock_svc.create.return_value = _make_post()
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.post("/api/posts", json=self._payload)

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "uuid-1"
        assert body["slug"] == "test-post"
        app.dependency_overrides.clear()

    def test_duplicate_slug_returns_409(self, client, app):
        mock_svc = MagicMock()
        mock_svc.create.side_effect = DuplicateSlugError(
            '이미 같은 제목의 글이 있습니다 (slug="test-post"). 제목을 바꿔주세요.'
        )
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.post("/api/posts", json=self._payload)

        assert resp.status_code == 409
        # Frontend renders the backend detail verbatim — make sure the slug
        # name is in the message so users see what collided.
        assert "test-post" in resp.json()["detail"]
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

    # BUG-10: editing a draft must forward category / album_ids / artist_ids
    # to the service. Prior to the fix UpdatePostRequest dropped them silently.
    def test_update_forwards_category_album_artist(self, client, app):
        mock_svc = MagicMock()
        mock_svc.update.return_value = _make_post(slug="updated-post")
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.put(
            "/api/posts/uuid-1",
            json={
                "category": "jazz",
                "album_ids": ["a1", "a2"],
                "artist_ids": ["ar1"],
            },
        )

        assert resp.status_code == 200
        kwargs = mock_svc.update.call_args.kwargs
        assert kwargs["category"] == "jazz"
        assert kwargs["album_ids"] == ["a1", "a2"]
        assert kwargs["artist_ids"] == ["ar1"]
        app.dependency_overrides.clear()

    def test_update_omits_unset_fields(self, client, app):
        mock_svc = MagicMock()
        mock_svc.update.return_value = _make_post()
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.put("/api/posts/uuid-1", json={"title": "Only Title"})

        assert resp.status_code == 200
        kwargs = mock_svc.update.call_args.kwargs
        assert kwargs == {"title": "Only Title"}
        app.dependency_overrides.clear()
