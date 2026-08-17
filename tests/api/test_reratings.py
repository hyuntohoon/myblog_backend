"""FEAT-album-rerating — route-level contract for 재평가.

The load-bearing property is a PRIVACY split, and it is the whole reason there
are two response schemas over one table: the 재평가 중 list is PUBLIC (owner
decision — seeing that someone pulled an album back for another listen belongs on
their profile), while the withdrawn score behind it is AUTHOR-ONLY. Publishing
`previous_rating` would contradict the feature's own premise that the star is
gone, so the public payload must carry no trace of it.

The transactional half (withdraw/restore/complete against the real CHECKs) is
pinned in tests/integration/test_rerating_service_db.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.auth import require_cognito_token

_TS = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
_SUB = "0468fd3c-1111-2222-3333-444455556666"
_ALBUM = "11111111-2222-3333-4444-555555555555"


def _pending(album_id=_ALBUM, previous_rating=3.5, previous_comment="이전 한 줄"):
    return SimpleNamespace(
        album_id=uuid.UUID(album_id),
        previous_rating=previous_rating,
        previous_comment=previous_comment,
        created_at=_TS,
    )


def _album(album_id=_ALBUM, title="Indigo"):
    return SimpleNamespace(id=uuid.UUID(album_id), title=title, cover_url=None)


def _override(app, svc):
    from app.db.session import get_db
    from app.di import get_rerating_service

    app.dependency_overrides[require_cognito_token] = lambda: {"sub": _SUB}
    app.dependency_overrides[get_rerating_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestMyReratings:
    def test_own_list_carries_the_withdrawn_score(self, client, app):
        """The author needs it: it powers the 이전 ★ hint and is what 재평가 취소
        restores."""
        svc = MagicMock()
        svc.list_pending.return_value = [(_pending(), _album())]
        _override(app, svc)

        resp = client.get("/api/me/reratings")

        assert resp.status_code == 200
        row = resp.json()["reratings"][0]
        assert row["album_id"] == _ALBUM
        assert row["previous_rating"] == 3.5
        assert row["previous_comment"] == "이전 한 줄"
        app.dependency_overrides.clear()

    def test_start_is_204(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        resp = client.put(f"/api/me/reratings/{_ALBUM}")

        assert resp.status_code == 204
        assert svc.start.call_count == 1
        app.dependency_overrides.clear()

    def test_start_without_a_rating_is_409(self, client, app):
        """재평가 means redoing a finished 평가. Nothing to redo is a conflict, not
        a 404 — the album exists, the 평가 never did."""
        from app.services.rerating_service import NoRatingToRerateError

        svc = MagicMock()
        svc.start.side_effect = NoRatingToRerateError(_ALBUM)
        _override(app, svc)

        resp = client.put(f"/api/me/reratings/{_ALBUM}")

        assert resp.status_code == 409
        app.dependency_overrides.clear()

    def test_start_on_a_missing_album_is_404(self, client, app):
        from app.services.rating_service import AlbumNotFoundError

        svc = MagicMock()
        svc.start.side_effect = AlbumNotFoundError(_ALBUM)
        _override(app, svc)

        resp = client.put(f"/api/me/reratings/{_ALBUM}")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_cancel_is_204_even_when_nothing_is_open(self, client, app):
        svc = MagicMock()
        svc.cancel.return_value = None
        _override(app, svc)

        resp = client.delete(f"/api/me/reratings/{_ALBUM}")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_malformed_album_id_is_404_not_500(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        assert client.put("/api/me/reratings/not-a-uuid").status_code == 404
        assert client.delete("/api/me/reratings/not-a-uuid").status_code == 404
        assert svc.start.call_count == 0
        app.dependency_overrides.clear()


class TestPublicProfileReratings:
    def _override_public(self, app, ratings_svc, rerating_svc):
        from app.db.session import get_db
        from app.di import get_rating_service, get_rerating_service

        app.dependency_overrides[get_rating_service] = lambda: ratings_svc
        app.dependency_overrides[get_rerating_service] = lambda: rerating_svc
        app.dependency_overrides[get_db] = lambda: MagicMock()

    def test_public_profile_lists_reratings_without_the_score(self, client, app):
        """The privacy line of this whole feature: the list is public, the
        withdrawn score is not. A regression that leaks it publishes a verdict
        its author explicitly withdrew."""
        user = SimpleNamespace(
            id=uuid.UUID(_SUB), handle="rj", display_name="RJ",
            avatar_url=None, created_at=_TS,
        )
        ratings_svc = MagicMock()
        ratings_svc.member_profile.return_value = (user, [])
        rerating_svc = MagicMock()
        rerating_svc.list_pending.return_value = [(_pending(), _album())]
        self._override_public(app, ratings_svc, rerating_svc)

        resp = client.get("/api/members/rj")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["reratings"]) == 1
        row = data["reratings"][0]
        assert row["album_id"] == _ALBUM and row["album_title"] == "Indigo"
        assert "previous_rating" not in row
        assert "previous_comment" not in row
        # A withdrawn 평가 is gone from the rated feed, and the count follows it.
        assert data["reviews"] == [] and data["review_count"] == 0
        app.dependency_overrides.clear()

    def test_reratings_are_resolved_for_the_profile_user_not_the_caller(self, client, app):
        """A public surface has no acting member. The list must be keyed on the
        profile's own user id — reading it off a caller would show visitors their
        own 재평가 on someone else's page."""
        user = SimpleNamespace(
            id=uuid.UUID(_SUB), handle="rj", display_name="RJ",
            avatar_url=None, created_at=_TS,
        )
        ratings_svc = MagicMock()
        ratings_svc.member_profile.return_value = (user, [])
        rerating_svc = MagicMock()
        rerating_svc.list_pending.return_value = []
        self._override_public(app, ratings_svc, rerating_svc)

        client.get("/api/members/rj")

        assert rerating_svc.list_pending.call_args[0][1] == user.id
        app.dependency_overrides.clear()
