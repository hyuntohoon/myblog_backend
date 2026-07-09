"""FEAT-today-buckit Step 4 — todays-pick store route unit tests.

Mirrors test_genres.py: the service is mocked (these are route tests, not DB
tests — the upsert/get_history mapping is covered via call assertions; a
DB-backed integration test would need a live Postgres). Owner-gate behavior is
locked at the route layer following test_publish.py / test_auth_failclosed.py.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from app.di import get_todays_pick_service
from app.services.todays_pick_service import TodaysPickService

_TRACK = uuid.uuid4()
_ALBUM = uuid.uuid4()


def _pick(
    *,
    pid="pick-1",
    pick_date=date(2026, 7, 9),
    track_id=_TRACK,
    album_id=_ALBUM,
    title="Dior",
    artist="Pop Smoke",
    cover_url="https://i.scdn.co/cover.jpg",
    spotify_track_id="0VjIjW4GlU",
):
    p = MagicMock()  # bare mock — response_model reads attrs via from_attributes
    p.id = pid
    p.pick_date = pick_date
    p.track_id = track_id
    p.album_id = album_id
    p.title = title
    p.artist = artist
    p.cover_url = cover_url
    p.spotify_track_id = spotify_track_id
    p.created_at = datetime(2026, 7, 9, 12, 0, 0)
    p.updated_at = datetime(2026, 7, 9, 12, 0, 0)
    return p


def _override(app, svc):
    # Import get_db lazily so a module-top import doesn't pull app.core.config at
    # collection time (empty settings singleton — reference-backend-test-config-import).
    from app.db.session import get_db

    app.dependency_overrides[get_todays_pick_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


# ── GET /api/todays-pick ────────────────────────────────────────────────────────


class TestGetTodaysPick:
    def test_returns_today_pick_when_posted(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        svc.get_today.return_value = _pick()
        _override(app, svc)

        resp = client.get("/api/todays-pick")

        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Dior"
        assert data["artist"] == "Pop Smoke"
        assert data["spotify_track_id"] == "0VjIjW4GlU"
        # UUID/date columns serialize to str/ISO — the response_model does the cast.
        assert data["track_id"] == str(_TRACK)
        assert data["album_id"] == str(_ALBUM)
        svc.get_today.assert_called_once()
        app.dependency_overrides.clear()

    def test_returns_null_on_no_pick_day(self, client, app):
        # A no-pick day is a NORMAL state — 200 + null (NOT a 404); the home tile
        # hides on null.
        svc = MagicMock(spec=TodaysPickService)
        svc.get_today.return_value = None
        _override(app, svc)

        resp = client.get("/api/todays-pick")

        assert resp.status_code == 200
        assert resp.json() is None
        app.dependency_overrides.clear()


# ── GET /api/todays-pick/history ────────────────────────────────────────────────


class TestGetTodaysPickHistory:
    def test_lists_picks_date_desc(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        svc.list_history.return_value = [_pick(pid="a"), _pick(pid="b")]
        _override(app, svc)

        resp = client.get("/api/todays-pick/history")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == "a"
        _, kwargs = svc.list_history.call_args
        assert kwargs == {"limit": 30, "before": None}
        app.dependency_overrides.clear()

    def test_passes_limit_and_before(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        svc.list_history.return_value = []
        _override(app, svc)

        resp = client.get("/api/todays-pick/history?limit=5&before=2026-07-01")

        assert resp.status_code == 200
        assert resp.json() == []
        _, kwargs = svc.list_history.call_args
        assert kwargs == {"limit": 5, "before": date(2026, 7, 1)}
        app.dependency_overrides.clear()

    def test_limit_above_max_is_422(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)

        resp = client.get("/api/todays-pick/history?limit=101")

        assert resp.status_code == 422
        svc.list_history.assert_not_called()
        app.dependency_overrides.clear()

    def test_limit_below_one_is_422(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)

        resp = client.get("/api/todays-pick/history?limit=0")

        assert resp.status_code == 422
        app.dependency_overrides.clear()


# ── PUT /api/todays-pick ────────────────────────────────────────────────────────


class TestPutTodaysPick:
    def test_put_upserts_and_returns_item(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        svc.upsert.return_value = _pick(title="Dior")
        _override(app, svc)

        body = {
            "track_id": str(_TRACK),
            "album_id": str(_ALBUM),
            "title": "Dior",
            "artist": "Pop Smoke",
            "cover_url": "https://i.scdn.co/cover.jpg",
            "spotify_track_id": "0VjIjW4GlU",
        }
        resp = client.put("/api/todays-pick", json=body)

        assert resp.status_code == 200
        assert resp.json()["title"] == "Dior"
        _, kwargs = svc.upsert.call_args
        # The server pins pick_date to today — it is NOT in the upsert call args.
        assert "pick_date" not in kwargs
        assert kwargs["track_id"] == str(_TRACK)
        assert kwargs["album_id"] == str(_ALBUM)
        assert kwargs["spotify_track_id"] == "0VjIjW4GlU"
        app.dependency_overrides.clear()

    def test_put_accepts_missing_cover_url(self, client, app):
        # cover_url is nullable (album art may be missing from Spotify).
        svc = MagicMock(spec=TodaysPickService)
        svc.upsert.return_value = _pick(cover_url=None)
        _override(app, svc)

        body = {
            "track_id": str(_TRACK),
            "album_id": str(_ALBUM),
            "title": "Untitled",
            "artist": "Unknown",
            "spotify_track_id": "abc",
        }
        resp = client.put("/api/todays-pick", json=body)

        assert resp.status_code == 200
        _, kwargs = svc.upsert.call_args
        assert kwargs["cover_url"] is None
        app.dependency_overrides.clear()

    def test_put_rejects_missing_track_id_422(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)

        body = {
            "album_id": str(_ALBUM),
            "title": "Dior",
            "artist": "Pop Smoke",
            "spotify_track_id": "0VjIjW4GlU",
        }
        resp = client.put("/api/todays-pick", json=body)

        assert resp.status_code == 422
        svc.upsert.assert_not_called()
        app.dependency_overrides.clear()

    def test_put_rejects_blank_title_422(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)

        body = {
            "track_id": str(_TRACK),
            "album_id": str(_ALBUM),
            "title": "",
            "artist": "Pop Smoke",
            "spotify_track_id": "0VjIjW4GlU",
        }
        resp = client.put("/api/todays-pick", json=body)

        assert resp.status_code == 422
        svc.upsert.assert_not_called()
        app.dependency_overrides.clear()

    def test_put_rejects_bad_uuid_422(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)

        body = {
            "track_id": "not-a-uuid",
            "album_id": str(_ALBUM),
            "title": "Dior",
            "artist": "Pop Smoke",
            "spotify_track_id": "0VjIjW4GlU",
        }
        resp = client.put("/api/todays-pick", json=body)

        assert resp.status_code == 422
        svc.upsert.assert_not_called()
        app.dependency_overrides.clear()


# ── DELETE /api/todays-pick ─────────────────────────────────────────────────────


class TestDeleteTodaysPick:
    def test_delete_returns_204_when_posted(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        svc.delete_today.return_value = True
        _override(app, svc)

        resp = client.delete("/api/todays-pick")

        assert resp.status_code == 204
        svc.delete_today.assert_called_once()
        app.dependency_overrides.clear()

    def test_delete_returns_404_when_nothing_posted(self, client, app):
        svc = MagicMock(spec=TodaysPickService)
        svc.delete_today.return_value = False
        _override(app, svc)

        resp = client.delete("/api/todays-pick")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


# ── Owner gate (require_owner) on PUT/DELETE ───────────────────────────────────
# The GETs are public (no auth dependency). The writes must fail closed in prod:
# missing OWNER_SUB → 503, valid-but-non-owner member → 403, local ENV bypasses.
# (test_publish.py / test_auth_failclosed.py pattern.)


def _prod_settings(owner_sub: str):
    """A prod settings stub that forces require_owner past the local bypass."""
    from types import SimpleNamespace

    return SimpleNamespace(
        ENV="prod",
        COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
        COGNITO_REGION="ap-northeast-2",
        OWNER_SUB=owner_sub,
    )


class TestOwnerGate:
    def test_put_503_when_owner_sub_unset_in_prod(self, client, app):
        # require_owner depends on require_cognito_token. To exercise the OWNER_SUB
        # fail-closed branch IN ISOLATION (independent of token presence), we stub
        # the token dependency to return claims, then assert require_owner itself
        # 503s on an unset OWNER_SUB (the misconfig guard). test_auth_failclosed
        # locks the same branch at the function level; this locks it at the route.
        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token

        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "anyone"}

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="")):
            resp = client.put(
                "/api/todays-pick",
                json={
                    "track_id": str(_TRACK),
                    "album_id": str(_ALBUM),
                    "title": "Dior",
                    "artist": "Pop Smoke",
                    "spotify_track_id": "0VjIjW4GlU",
                },
            )
        assert resp.status_code == 503
        svc.upsert.assert_not_called()
        app.dependency_overrides.clear()

    def test_put_403_for_non_owner_in_prod(self, client, app):
        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token

        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)
        # A valid, signed-in non-owner member token — require_owner must 403.
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "some-member"}

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.put(
                "/api/todays-pick",
                json={
                    "track_id": str(_TRACK),
                    "album_id": str(_ALBUM),
                    "title": "Dior",
                    "artist": "Pop Smoke",
                    "spotify_track_id": "0VjIjW4GlU",
                },
            )
        assert resp.status_code == 403
        svc.upsert.assert_not_called()
        app.dependency_overrides.clear()

    def test_put_allows_the_owner_in_prod(self, client, app):
        # The happy owner path in prod: a valid owner token reaches the service.
        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token

        svc = MagicMock(spec=TodaysPickService)
        svc.upsert.return_value = _pick()
        _override(app, svc)
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "owner-sub"}

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.put(
                "/api/todays-pick",
                json={
                    "track_id": str(_TRACK),
                    "album_id": str(_ALBUM),
                    "title": "Dior",
                    "artist": "Pop Smoke",
                    "spotify_track_id": "0VjIjW4GlU",
                },
            )
        assert resp.status_code == 200
        svc.upsert.assert_called_once()
        app.dependency_overrides.clear()

    def test_delete_503_when_owner_sub_unset_in_prod(self, client, app):
        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token

        svc = MagicMock(spec=TodaysPickService)
        _override(app, svc)
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "anyone"}

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="")):
            resp = client.delete("/api/todays-pick")
        assert resp.status_code == 503
        svc.delete_today.assert_not_called()
        app.dependency_overrides.clear()

    def test_writes_pass_in_local_env(self, client, app):
        # conftest forces ENV=local, so require_owner bypasses — owner writes
        # reach the service (this is the dev convenience the bypass preserves).
        svc = MagicMock(spec=TodaysPickService)
        svc.upsert.return_value = _pick()
        svc.delete_today.return_value = True
        _override(app, svc)

        put_resp = client.put(
            "/api/todays-pick",
            json={
                "track_id": str(_TRACK),
                "album_id": str(_ALBUM),
                "title": "Dior",
                "artist": "Pop Smoke",
                "spotify_track_id": "0VjIjW4GlU",
            },
        )
        del_resp = client.delete("/api/todays-pick")

        assert put_resp.status_code == 200
        assert del_resp.status_code == 204
        svc.upsert.assert_called_once()
        svc.delete_today.assert_called_once()
        app.dependency_overrides.clear()
