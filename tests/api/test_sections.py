from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from app.di import get_section_service
from app.services.post_service import PostService


class TestListSections:
    """STAB-5: GET /api/sections is read-only and returns the seeded set."""

    def test_returns_seeded_sections(self, client, app):
        mock_svc = MagicMock()
        mock_svc.list_sections.return_value = [
            {"name": "Reviews", "slug": "reviews"},
            {"name": "Best New Music", "slug": "best-new-music"},
            {"name": "Features", "slug": "features"},
            {"name": "Tracks", "slug": "tracks"},
        ]
        app.dependency_overrides[get_section_service] = lambda: mock_svc

        resp = client.get("/api/sections")

        assert resp.status_code == 200
        data = resp.json()
        assert [s["slug"] for s in data["sections"]] == [
            "reviews",
            "best-new-music",
            "features",
            "tracks",
        ]
        app.dependency_overrides.clear()

    def test_no_create_endpoint(self, client, app):
        # The unauthenticated POST create path (old POST /api/categories) is gone.
        # POST /api/sections must not exist (405, not 200/201).
        resp = client.post("/api/sections", json={"name": "Smuggled"})
        assert resp.status_code == 405


class TestUnknownSectionRejected:
    """STAB-5: get-or-create is removed; an unseeded section name is rejected."""

    def test_create_unknown_section_raises(self):
        section_repo = MagicMock()
        section_repo.get_by_name.return_value = None  # not a seeded section
        svc = PostService(post_repo=MagicMock(), section_repo=section_repo)

        with pytest.raises(ValueError, match="unknown section"):
            svc.create(
                MagicMock(),
                title="T",
                posted_date=date(2026, 6, 6),
                section_name="Nope",
            )

    def test_update_unknown_section_raises(self):
        section_repo = MagicMock()
        section_repo.get_by_name.return_value = None
        post_repo = MagicMock()
        post_repo.get_by_id.return_value = MagicMock()  # post exists
        svc = PostService(post_repo=post_repo, section_repo=section_repo)

        with pytest.raises(ValueError, match="unknown section"):
            svc.update(MagicMock(), "uuid-1", category="Nope")

    def test_empty_section_clears_to_null(self):
        # Empty string => section_id None (nullable FK), not a lookup/reject.
        section_repo = MagicMock()
        post_repo = MagicMock()
        post = MagicMock()
        post_repo.get_by_id.return_value = post
        post_repo.update.return_value = post
        svc = PostService(post_repo=post_repo, section_repo=section_repo)

        svc.update(MagicMock(), "uuid-1", category="")

        section_repo.get_by_name.assert_not_called()
        # section_id forwarded as None to the repo update
        assert post_repo.update.call_args.kwargs.get("section_id") is None
