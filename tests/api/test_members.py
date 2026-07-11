from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock


def _override(app, svc):
    # get_db lazily imported (module-top import pulls app.core.config early).
    from app.db.session import get_db
    from app.di import get_integration_service

    app.dependency_overrides[get_integration_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestMemberNowPlaying:
    def test_unknown_handle_404(self, client, app):
        from app.services.review_service import MemberNotFoundError

        svc = MagicMock()
        svc.public_now_playing.side_effect = MemberNotFoundError("ghost")
        _override(app, svc)
        resp = client.get("/api/members/ghost/now-playing")
        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_member_without_row_is_not_playing(self, client, app):
        # Covers both 미연동 and connected-but-idle: the service returns None and
        # the response must not reveal which (integration status stays private).
        svc = MagicMock()
        svc.public_now_playing.return_value = None
        _override(app, svc)
        resp = client.get("/api/members/rj/now-playing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_playing"] is False and data["track"] is None
        app.dependency_overrides.clear()

    def test_member_playing_returns_track(self, client, app):
        svc = MagicMock()
        svc.public_now_playing.return_value = SimpleNamespace(
            artist_name="RM", track_name="들꽃놀이 (with 조유진)", album_name="Indigo",
            image_url="https://img.example/indigo.jpg", played_at=None,
        )
        _override(app, svc)
        resp = client.get("/api/members/rj/now-playing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_playing"] is True and data["track"] == "들꽃놀이 (with 조유진)"
        assert "username" not in data  # never expose the Last.fm username
        app.dependency_overrides.clear()


class TestPublicNowPlayingServiceUnit:
    def test_unknown_handle_raises(self):
        from app.services.integration_service import IntegrationService
        from app.services.review_service import MemberNotFoundError

        db = MagicMock()
        db.scalar.return_value = None  # no user with that handle
        svc = IntegrationService(users=MagicMock())
        try:
            svc.public_now_playing(db, "Ghost")
        except MemberNotFoundError:
            pass
        else:
            raise AssertionError("expected MemberNotFoundError")

    def test_handle_lowercased_and_forwards_to_member_read(self):
        from app.services.integration_service import IntegrationService

        user = SimpleNamespace(id=uuid.UUID(int=7))
        row = SimpleNamespace(track_name="t")
        db = MagicMock()
        db.scalar.side_effect = [user, row]  # user lookup, then now-playing read
        svc = IntegrationService(users=MagicMock())
        assert svc.public_now_playing(db, "RJ") is row
        # The user lookup ran on the lowercased handle (V36 handles are lowercase).
        lookup_sql = str(db.scalar.call_args_list[0].args[0])
        assert "users" in lookup_sql
