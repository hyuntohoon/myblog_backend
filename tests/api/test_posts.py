from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

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
    p.section = MagicMock()
    p.section.name = "music"
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

        # FEAT-post-edit-delete-ui Step 3 added an MDX-removal side-effect; this
        # test covers the DB/route contract, so isolate it (covered separately
        # in test_unpublish.py).
        with patch("app.api.routes.posts.content_sync.remove_post_content"):
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

        with patch("app.api.routes.posts.content_sync.remove_post_content"):
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


class TestHardDeleteBucketItemCascade:
    """D22: a hard post delete must also remove review_bucket_items pointing at
    the post (post_id FK is ON DELETE SET NULL, so without this they'd orphan).
    Soft delete must NOT touch bucket items. Mock-level proof that the service
    issues the bucket-item delete before delete_by_id, on the hard path only.
    A real-engine cascade assertion lives in tests/integration/."""

    def test_hard_delete_removes_matching_bucket_items(self):
        from app.services.post_service import PostService
        from myblog_shared_db.models import ReviewBucketItem

        db = MagicMock()
        post_repo = MagicMock()
        post_repo.delete_by_id.return_value = True
        svc = PostService(post_repo=post_repo, section_repo=MagicMock())

        result = svc.delete(db, "uuid-1", hard=True)

        assert result is True
        # The bucket-item delete was queued, filtered on post_id, with
        # synchronize_session=False, and the post delete still ran (single tx).
        db.query.assert_called_once_with(ReviewBucketItem)
        db.query.return_value.filter.return_value.delete.assert_called_once_with(
            synchronize_session=False
        )
        post_repo.delete_by_id.assert_called_once_with(db, "uuid-1")

    def test_soft_delete_does_not_touch_bucket_items(self):
        from app.services.post_service import PostService

        db = MagicMock()
        post_repo = MagicMock()
        post_repo.set_status.return_value = _make_post(status="archived")
        svc = PostService(post_repo=post_repo, section_repo=MagicMock())

        svc.delete(db, "uuid-1", hard=False)

        # No bucket-item query on the soft path.
        db.query.assert_not_called()
        post_repo.set_status.assert_called_once_with(db, "uuid-1", "archived")


class TestRestorePost:
    def test_restore_existing_archived_returns_200(self, client, app):
        mock_svc = MagicMock()
        mock_svc.restore.return_value = _make_post(status="published")
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        # Restore now re-publishes the MDX (Step 3); isolate that side-effect
        # here — re-publish is covered in test_unpublish.py.
        with patch("app.api.routes.posts.content_sync.republish_post_content"):
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

    def test_missing_jwt_returns_401_in_prod_env(self, client):
        # create_post must be Cognito-gated like every other mutating route
        # (it previously omitted the dependency — audit S3). Mirror the publish
        # JWT test: in prod env with no token, the request is rejected before
        # reaching the service.
        from unittest.mock import patch
        import app.core.auth as auth_module

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.post("/api/posts", json=self._payload)

        assert resp.status_code == 401


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

    def test_update_rating_two_decimal_round_trips(self, client, app):
        """FEAT-view-redesign Step 4: posts.rating widened to Numeric(3,2).

        Pydantic schema has ge=0, le=5 with no step constraint, so a value
        like 4.69 must reach the service exactly (no rounding to 4.7).
        Pairs with the v0.4.0 shared_db pin so the column can hold it.
        """
        mock_svc = MagicMock()
        mock_svc.update.return_value = _make_post(slug="rated-post")
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.put("/api/posts/uuid-1", json={"rating": 4.69})

        assert resp.status_code == 200
        kwargs = mock_svc.update.call_args.kwargs
        assert kwargs["rating"] == 4.69
        app.dependency_overrides.clear()

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

    def test_update_forwards_recommended_track_ids(self, client, app):
        mock_svc = MagicMock()
        mock_svc.update.return_value = _make_post(slug="updated-post")
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.put(
            "/api/posts/uuid-1",
            json={"recommended_track_ids": ["t1", "t2"]},
        )

        assert resp.status_code == 200
        kwargs = mock_svc.update.call_args.kwargs
        # FEAT-view-redesign Step 3: set of picked track IDs (no position/note).
        assert kwargs["recommended_track_ids"] == ["t1", "t2"]
        app.dependency_overrides.clear()

    def test_update_empty_recommended_track_ids_is_explicit_clear(self, client, app):
        mock_svc = MagicMock()
        mock_svc.update.return_value = _make_post()
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        # Sending [] (set but empty) must reach the service as an explicit
        # clear, not be treated as "not provided". Mirrors album_ids: [] semantics.
        resp = client.put("/api/posts/uuid-1", json={"recommended_track_ids": []})

        assert resp.status_code == 200
        kwargs = mock_svc.update.call_args.kwargs
        assert kwargs["recommended_track_ids"] == []
        app.dependency_overrides.clear()


class TestGetPost:
    @staticmethod
    def _post_with_relations(
        post_id="uuid-1",
        slug="test-post",
        albums=(),
        artists=(),
    ):
        p = MagicMock()
        p.id = post_id
        p.slug = slug
        p.title = "Test Post"
        p.description = "desc"
        p.body_mdx = "body"
        p.status = "published"
        p.posted_date = date(2026, 5, 27)
        p.rating = 4.0
        p.section = MagicMock()
        p.section.name = "music"
        p.albums = [MagicMock(id=aid) for aid in albums]
        p.artists = [MagicMock(id=aid) for aid in artists]
        return p

    def test_get_returns_recommended_track_ids_in_response(self, client, app):
        mock_svc = MagicMock()
        mock_svc.get_by_id.return_value = self._post_with_relations(
            albums=["a1"], artists=["ar1"]
        )
        mock_svc.list_recommended_track_ids.return_value = ["t1", "t2"]
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts/uuid-1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["recommended_track_ids"] == ["t1", "t2"]
        mock_svc.list_recommended_track_ids.assert_called_once()
        app.dependency_overrides.clear()

    def test_get_returns_empty_recommended_track_ids_when_none(self, client, app):
        mock_svc = MagicMock()
        mock_svc.get_by_id.return_value = self._post_with_relations()
        mock_svc.list_recommended_track_ids.return_value = []
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts/uuid-1")

        assert resp.status_code == 200
        assert resp.json()["recommended_track_ids"] == []
        app.dependency_overrides.clear()

    def test_get_nonexistent_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.get_by_id.return_value = None
        app.dependency_overrides[get_post_service] = lambda: mock_svc

        resp = client.get("/api/posts/no-such-id")

        assert resp.status_code == 404
        mock_svc.list_recommended_track_ids.assert_not_called()
        app.dependency_overrides.clear()
