from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

_TS = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _override(app, svc):
    # get_db lazily imported (module-top import pulls app.core.config early).
    from app.db.session import get_db
    from app.di import get_integration_service

    app.dependency_overrides[get_integration_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestMemberNowPlaying:
    def test_unknown_handle_404(self, client, app):
        from app.services.rating_service import MemberNotFoundError

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

    def test_member_playing_returns_track_with_provenance(self, client, app):
        from app.services.integration_service import PublicNowPlaying

        svc = MagicMock()
        svc.public_now_playing.return_value = PublicNowPlaying(
            source="lastfm", artist="RM", track="들꽃놀이 (with 조유진)", album="Indigo",
            image_url="https://img.example/indigo.jpg", played_at=None,
            source_username="rj_listens",
        )
        _override(app, svc)
        resp = client.get("/api/members/rj/now-playing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_playing"] is True and data["track"] == "들꽃놀이 (with 조유진)"
        # OQ7 provenance: Last.fm is an unverified username-only connect, so the
        # public payload says where the data comes from ("via Last.fm @x").
        assert data["source"] == "lastfm"
        assert data["source_username"] == "rj_listens"
        app.dependency_overrides.clear()

    def test_spotify_pick_has_no_username(self, client, app):
        from app.services.integration_service import PublicNowPlaying

        svc = MagicMock()
        svc.public_now_playing.return_value = PublicNowPlaying(
            source="spotify", artist="Daniel Caesar", track="Always", album="NEVER ENOUGH",
            image_url=None, played_at=None, source_username=None,
        )
        _override(app, svc)
        resp = client.get("/api/members/rj/now-playing")
        data = resp.json()
        # Spotify connects are OAuth-proven — no username exposure.
        assert data["source"] == "spotify" and data["source_username"] is None
        app.dependency_overrides.clear()

    def test_idle_collapse_carries_no_provenance(self, client, app):
        # Privacy: when nothing is playing the payload must not reveal WHICH
        # provider is connected (or that any is) — source fields stay null.
        svc = MagicMock()
        svc.public_now_playing.return_value = None
        _override(app, svc)
        data = client.get("/api/members/rj/now-playing").json()
        assert data["is_playing"] is False
        assert data["source"] is None and data["source_username"] is None
        app.dependency_overrides.clear()


def _svc_db(user, lastfm_row, spotify_row, username="lfm_user"):
    """MagicMock db wired for public_now_playing's call order:
    scalar(user) → scalar(lastfm now-playing) → get(spotify singleton) →
    scalar(username) [lastfm picks only]."""
    db = MagicMock()
    db.scalar.side_effect = [user, lastfm_row, username]
    db.get.return_value = spotify_row
    return db


class TestPublicNowPlayingServiceUnit:
    def test_unknown_handle_raises(self):
        from app.services.integration_service import IntegrationService
        from app.services.rating_service import MemberNotFoundError

        db = MagicMock()
        db.scalar.return_value = None  # no user with that handle
        svc = IntegrationService(users=MagicMock())
        try:
            svc.public_now_playing(db, "Ghost")
        except MemberNotFoundError:
            pass
        else:
            raise AssertionError("expected MemberNotFoundError")

    def test_lastfm_only_picks_lastfm_with_username(self):
        from app.services.integration_service import IntegrationService

        user = SimpleNamespace(id=uuid.UUID(int=7))
        row = SimpleNamespace(
            artist_name="a", track_name="t", album_name="al", image_url=None,
            played_at=None, created_at=_TS,
        )
        db = _svc_db(user, row, spotify_row=None, username="celeb_handle")
        svc = IntegrationService(users=MagicMock())
        pick = svc.public_now_playing(db, "RJ")
        assert pick is not None
        assert pick.source == "lastfm" and pick.track == "t"
        assert pick.source_username == "celeb_handle"
        # The user lookup ran on the lowercased handle (V36 handles are lowercase).
        lookup_sql = str(db.scalar.call_args_list[0].args[0])
        assert "users" in lookup_sql

    def test_spotify_only_picks_spotify_without_username(self):
        from app.services.integration_service import IntegrationService

        user = SimpleNamespace(id=uuid.UUID(int=7))
        sp = SimpleNamespace(
            is_playing=True, artist_name="sa", track_name="st", album_name="sal",
            image_url="i", updated_at=_TS,
        )
        db = _svc_db(user, lastfm_row=None, spotify_row=sp)
        svc = IntegrationService(users=MagicMock())
        pick = svc.public_now_playing(db, "rj")
        assert pick is not None
        assert pick.source == "spotify" and pick.track == "st"
        assert pick.source_username is None and pick.played_at is None

    def test_both_playing_most_recent_wins(self):
        from datetime import timedelta

        from app.services.integration_service import IntegrationService

        user = SimpleNamespace(id=uuid.UUID(int=7))
        lf = SimpleNamespace(
            artist_name="a", track_name="lf", album_name=None, image_url=None,
            played_at=None, created_at=_TS,
        )
        sp_newer = SimpleNamespace(
            is_playing=True, artist_name="sa", track_name="sp", album_name=None,
            image_url=None, updated_at=_TS + timedelta(minutes=5),
        )
        db = _svc_db(user, lf, sp_newer)
        svc = IntegrationService(users=MagicMock())
        pick = svc.public_now_playing(db, "rj")
        assert pick is not None and pick.source == "spotify"

        # Flip recency → Last.fm wins (and the username read happens).
        sp_older = SimpleNamespace(
            is_playing=True, artist_name="sa", track_name="sp", album_name=None,
            image_url=None, updated_at=_TS - timedelta(minutes=5),
        )
        db = _svc_db(user, lf, sp_older, username="u")
        pick = svc.public_now_playing(db, "rj")
        assert pick is not None and pick.source == "lastfm" and pick.source_username == "u"

    def test_spotify_idle_and_no_lastfm_is_none(self):
        from app.services.integration_service import IntegrationService

        user = SimpleNamespace(id=uuid.UUID(int=7))
        sp_idle = SimpleNamespace(is_playing=False, updated_at=_TS)
        db = _svc_db(user, lastfm_row=None, spotify_row=sp_idle)
        svc = IntegrationService(users=MagicMock())
        assert svc.public_now_playing(db, "rj") is None
