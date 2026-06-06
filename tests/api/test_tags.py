from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from app.di import get_tag_service
from app.services.post_service import PostService


def _tag(name: str, slug: str):
    """A stand-in Tag row (only .name is read back into responses)."""
    t = MagicMock()
    t.name = name
    t.slug = slug
    return t


class TestListTags:
    """STAB-5 Step 4: GET /api/tags is read-only and returns the seeded set."""

    def test_returns_seeded_tags(self, client, app):
        mock_svc = MagicMock()
        mock_svc.list_tags.return_value = [
            {"name": "album review", "slug": "album-review"},
            {"name": "track review", "slug": "track-review"},
            {"name": "reissue", "slug": "reissue"},
            {"name": "best album", "slug": "best-album"},
            {"name": "year-end list", "slug": "year-end-list"},
        ]
        app.dependency_overrides[get_tag_service] = lambda: mock_svc

        resp = client.get("/api/tags")

        assert resp.status_code == 200
        data = resp.json()
        assert [t["slug"] for t in data["tags"]] == [
            "album-review",
            "track-review",
            "reissue",
            "best-album",
            "year-end-list",
        ]
        app.dependency_overrides.clear()

    def test_no_create_endpoint(self, client, app):
        # Mirrors sections: there is no create path (read-only seeded vocab).
        resp = client.post("/api/tags", json={"name": "Smuggled"})
        assert resp.status_code == 405


class TestUnknownTagRejected:
    """STAB-5 Step 4: get-or-create is absent; an unseeded tag name is rejected."""

    def test_create_unknown_tag_raises(self):
        tag_repo = MagicMock()
        tag_repo.get_many_by_names.return_value = []  # none of the names seeded
        svc = PostService(
            post_repo=MagicMock(), section_repo=MagicMock(), tag_repo=tag_repo
        )

        with pytest.raises(ValueError, match="unknown tag"):
            svc.create(
                MagicMock(),
                title="T",
                posted_date=date(2026, 6, 6),
                tags=["not-a-real-tag"],
            )

    def test_update_unknown_tag_raises(self):
        tag_repo = MagicMock()
        tag_repo.get_many_by_names.return_value = []
        post_repo = MagicMock()
        post_repo.get_by_id.return_value = MagicMock()  # post exists
        svc = PostService(
            post_repo=post_repo, section_repo=MagicMock(), tag_repo=tag_repo
        )

        with pytest.raises(ValueError, match="unknown tag"):
            svc.update(MagicMock(), "uuid-1", tags=["nope"])

    def test_partial_unknown_rejects_whole_set(self):
        # One known + one unknown ⇒ reject (no partial attach).
        tag_repo = MagicMock()
        tag_repo.get_many_by_names.return_value = [_tag("reissue", "reissue")]
        svc = PostService(
            post_repo=MagicMock(), section_repo=MagicMock(), tag_repo=tag_repo
        )

        with pytest.raises(ValueError, match="ghost"):
            svc.create(
                MagicMock(),
                title="T",
                posted_date=date(2026, 6, 6),
                tags=["reissue", "ghost"],
            )


class TestTagResolution:
    """_resolve_tags normalizes input and enforces the seeded vocabulary."""

    def test_empty_and_none_resolve_to_no_tags(self):
        tag_repo = MagicMock()
        svc = PostService(
            post_repo=MagicMock(), section_repo=MagicMock(), tag_repo=tag_repo
        )

        assert svc._resolve_tags(MagicMock(), None) == []
        assert svc._resolve_tags(MagicMock(), []) == []
        assert svc._resolve_tags(MagicMock(), ["", "  "]) == []
        tag_repo.get_many_by_names.assert_not_called()

    def test_dedupes_and_strips_before_lookup(self):
        tag_repo = MagicMock()
        tag_repo.get_many_by_names.return_value = [_tag("reissue", "reissue")]
        svc = PostService(
            post_repo=MagicMock(), section_repo=MagicMock(), tag_repo=tag_repo
        )

        out = svc._resolve_tags(MagicMock(), ["reissue", " reissue ", "reissue"])

        # Looked up once, de-duped to the single distinct name.
        called_names = tag_repo.get_many_by_names.call_args.args[1]
        assert called_names == ["reissue"]
        assert [t.name for t in out] == ["reissue"]
