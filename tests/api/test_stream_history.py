"""FEAT-listening-history-import Step 4 — lifetime stream-history ranking routes
(mocked LibraryService). Ungated count/time top tracks/artists over the imported
Spotify Extended Streaming History; the `unit` field names the Count↔Time axis."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.di import get_library_service
from app.services.user_service import LOCAL_DEV_USER_ID


def _override(app, svc):
    from app.db.session import get_db

    app.dependency_overrides[get_library_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestStreamHistoryTopTracks:
    def test_count_metric_default(self, client, app):
        svc = MagicMock()
        svc.stream_history_top_tracks.return_value = {
            "items": [
                {"label": "Track A", "artist": "Artist A",
                 "spotify_track_uri": "spotify:track:aaa", "value": 53},
                {"label": "Track B", "artist": "Artist B",
                 "spotify_track_uri": "spotify:track:bbb", "value": 41},
            ],
            "unit": "count",
            "total_streams": 4500,
            "total_ms": 670_000_000,
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc),
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/top-tracks")

        assert resp.status_code == 200
        body = resp.json()
        assert body["unit"] == "count"
        assert body["total_streams"] == 4500
        assert body["items"][0]["value"] == 53
        assert body["items"][0]["spotify_track_uri"] == "spotify:track:aaa"
        # default metric=count, default limit=15, no range; user_id = the acting
        # member (local env → the all-zeros dev id)
        _, kwargs = svc.stream_history_top_tracks.call_args
        assert kwargs == {"user_id": LOCAL_DEV_USER_ID, "metric": "count",
                          "limit": 15, "frm": None, "to": None}

    def test_live_streams_passes_through(self, client, app):
        # FEAT-listening-live-merge: the live-tail count rides through to the contract.
        svc = MagicMock()
        svc.stream_history_top_tracks.return_value = {
            "items": [], "unit": "count", "total_streams": 3252, "total_ms": 628_000_000,
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc), "live_streams": 37,
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/top-tracks")

        assert resp.status_code == 200
        assert resp.json()["live_streams"] == 37

    def test_time_metric_passes_through(self, client, app):
        svc = MagicMock()
        svc.stream_history_top_tracks.return_value = {
            "items": [{"label": "Long One", "artist": "X",
                       "spotify_track_uri": "spotify:track:zzz", "value": 9_000_000}],
            "unit": "ms",
            "total_streams": 4500,
            "total_ms": 670_000_000,
            "as_of": None,
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/top-tracks?metric=time&limit=5")

        assert resp.status_code == 200
        assert resp.json()["unit"] == "ms"
        _, kwargs = svc.stream_history_top_tracks.call_args
        assert kwargs == {"user_id": LOCAL_DEV_USER_ID, "metric": "time",
                          "limit": 5, "frm": None, "to": None}

    def test_bad_metric_is_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        resp = client.get("/api/library/stream-history/top-tracks?metric=bogus")

        assert resp.status_code == 422
        svc.stream_history_top_tracks.assert_not_called()

    def test_limit_is_clamped(self, client, app):
        svc = MagicMock()
        svc.stream_history_top_tracks.return_value = {
            "items": [], "unit": "count", "total_streams": 0, "total_ms": 0, "as_of": None,
        }
        _override(app, svc)

        client.get("/api/library/stream-history/top-tracks?limit=9999")
        _, kwargs = svc.stream_history_top_tracks.call_args
        assert kwargs["limit"] == 100  # clamped to the 100 ceiling


class TestStreamHistoryTopArtists:
    def test_artist_items_have_no_track_fields(self, client, app):
        svc = MagicMock()
        svc.stream_history_top_artists.return_value = {
            "items": [{"label": "Kid Milli", "value": 316}],
            "unit": "count",
            "total_streams": 4500,
            "total_ms": 670_000_000,
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc),
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/top-artists")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["label"] == "Kid Milli"
        assert item["value"] == 316
        assert item["artist"] is None
        assert item["spotify_track_uri"] is None


# ── Step 5: GATED panels (album / genre / era / retrospective) ──────────────────────

class TestStreamHistoryGenreEra:
    def test_genre_distribution_carries_unclassified(self, client, app):
        svc = MagicMock()
        svc.stream_history_genre_distribution.return_value = {
            "items": [{"label": "Hip-Hop", "value": 800}, {"label": "Rock", "value": 500}],
            "unit": "count",
            "unclassified": 120,  # ungenred + uncatalogued
            "total_streams": 3215,
            "total_ms": 620_000_000,
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc),
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/genre-distribution?metric=count")

        assert resp.status_code == 200
        body = resp.json()
        assert body["unclassified"] == 120
        assert body["items"][0]["label"] == "Hip-Hop"
        _, kwargs = svc.stream_history_genre_distribution.call_args
        assert kwargs == {"user_id": LOCAL_DEV_USER_ID, "metric": "count",
                          "frm": None, "to": None}

    def test_era_distribution_time_metric(self, client, app):
        svc = MagicMock()
        svc.stream_history_era_distribution.return_value = {
            "items": [{"label": "2010s", "value": 90_000_000}, {"label": "2020s", "value": 120_000_000}],
            "unit": "ms",
            "unclassified": 0,
            "total_streams": 3215,
            "total_ms": 620_000_000,
            "as_of": None,
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/era-distribution?metric=time")

        assert resp.status_code == 200
        assert resp.json()["unit"] == "ms"
        assert [i["label"] for i in resp.json()["items"]] == ["2010s", "2020s"]


class TestStreamHistoryTopAlbums:
    def test_albums_build_briefs(self, client, app):
        from datetime import date
        from types import SimpleNamespace

        album = SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            title="Some Album",
            cover_url="http://x/c.jpg",
            release_date=date(2021, 3, 1),
            popularity=55,
            artists=[SimpleNamespace(name="Artist A")],
        )
        svc = MagicMock()
        svc.stream_history_top_albums.return_value = {
            "items": [{"album_obj": album, "genres": ["Hip-Hop"], "value": 41}],
            "unit": "count",
            "unresolved": 14,
            "total_streams": 3215,
            "total_ms": 620_000_000,
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc),
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/top-albums")

        assert resp.status_code == 200
        body = resp.json()
        assert body["unresolved"] == 14
        item = body["items"][0]
        assert item["value"] == 41
        assert item["album"]["title"] == "Some Album"
        assert item["album"]["genres"] == ["Hip-Hop"]
        assert item["album"]["artist_names"] == ["Artist A"]

    def test_bad_metric_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        resp = client.get("/api/library/stream-history/top-albums?metric=nope")
        assert resp.status_code == 422
        svc.stream_history_top_albums.assert_not_called()


class TestStreamHistoryRetrospective:
    def test_retrospective_shape(self, client, app):
        svc = MagicMock()
        svc.stream_history_retrospective.return_value = {
            "per_year": [
                {"year": 2024, "plays": 100, "ms_played": 20_000_000},
                {"year": 2026, "plays": 3000, "ms_played": 600_000_000},
            ],
            "on_this_day": [
                {"year": 2024, "track_name": "Old Song", "artist_name": "X",
                 "album_id": None, "album_obj": None, "genres": [],
                 "plays": 3, "ms_played": 600_000},
            ],
            "today_kst": "06-23",
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc),
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/retrospective")

        assert resp.status_code == 200
        body = resp.json()
        assert body["today_kst"] == "06-23"
        assert body["per_year"][1]["year"] == 2026
        otd = body["on_this_day"][0]
        assert otd["year"] == 2024
        assert otd["album"] is None
        assert otd["track_name"] == "Old Song"


# ── FEAT-analysis-explore: time-range params ────────────────────────────────────────

class TestStreamHistoryRange:
    def test_from_to_parsed_and_forwarded(self, client, app):
        # The route parses ISO 8601 `from`/`to` → datetime and forwards them as frm/to.
        svc = MagicMock()
        svc.stream_history_top_tracks.return_value = {
            "items": [], "unit": "count", "total_streams": 0, "total_ms": 0, "as_of": None,
        }
        _override(app, svc)

        resp = client.get(
            "/api/library/stream-history/top-tracks"
            "?from=2025-01-01T00:00:00Z&to=2026-01-01T00:00:00Z"
        )

        assert resp.status_code == 200
        _, kwargs = svc.stream_history_top_tracks.call_args
        assert kwargs["frm"] == datetime(2025, 1, 1, tzinfo=timezone.utc)
        assert kwargs["to"] == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_range_threads_to_album_and_genre_panels(self, client, app):
        svc = MagicMock()
        svc.stream_history_genre_distribution.return_value = {
            "items": [], "unit": "count", "unclassified": 0,
            "total_streams": 0, "total_ms": 0, "as_of": None,
        }
        svc.stream_history_top_albums.return_value = {
            "items": [], "unit": "count", "unresolved": 0,
            "total_streams": 0, "total_ms": 0, "as_of": None,
        }
        _override(app, svc)

        client.get("/api/library/stream-history/genre-distribution?from=2024-01-01T00:00:00Z")
        _, gk = svc.stream_history_genre_distribution.call_args
        assert gk["frm"] == datetime(2024, 1, 1, tzinfo=timezone.utc) and gk["to"] is None

        client.get("/api/library/stream-history/top-albums?to=2025-06-01T00:00:00Z")
        _, ak = svc.stream_history_top_albums.call_args
        assert ak["to"] == datetime(2025, 6, 1, tzinfo=timezone.utc) and ak["frm"] is None

    def test_bad_from_is_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        resp = client.get("/api/library/stream-history/top-tracks?from=not-a-date")
        assert resp.status_code == 422
        svc.stream_history_top_tracks.assert_not_called()


# ── FEAT-analysis-explore: item drill-down ──────────────────────────────────────────

class TestStreamHistoryItem:
    def test_artist_detail_with_top_lists(self, client, app):
        svc = MagicMock()
        svc.stream_history_item_detail.return_value = {
            "type": "artist", "id": "Kid Milli", "label": "Kid Milli", "artist": None,
            "unit": "count", "count": 316, "time_ms": 51_000_000,
            "first_listen": datetime(2021, 1, 2, tzinfo=timezone.utc),
            "last_listen": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "per_year": [{"year": 2021, "plays": 100, "ms_played": 16_000_000}],
            "top_tracks": [{"label": "T1", "artist": "Kid Milli",
                            "spotify_track_uri": "spotify:track:aaa", "value": 40}],
            "top_albums": [],
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc), "live_streams": 3,
        }
        _override(app, svc)

        resp = client.get(
            "/api/library/stream-history/item?type=artist&id=Kid+Milli&metric=count"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "artist"
        assert body["count"] == 316 and body["live_streams"] == 3
        assert body["per_year"][0]["year"] == 2021
        assert body["top_tracks"][0]["spotify_track_uri"] == "spotify:track:aaa"
        _, kwargs = svc.stream_history_item_detail.call_args
        assert kwargs == {"user_id": LOCAL_DEV_USER_ID, "type_": "artist",
                          "id_": "Kid Milli", "metric": "count", "frm": None, "to": None}

    def test_album_detail_builds_brief(self, client, app):
        from datetime import date
        from types import SimpleNamespace

        album = SimpleNamespace(
            id="22222222-2222-2222-2222-222222222222", title="Detail Album",
            cover_url=None, release_date=date(2020, 1, 1), popularity=10,
            artists=[SimpleNamespace(name="A")],
        )
        svc = MagicMock()
        svc.stream_history_item_detail.return_value = {
            "type": "album", "id": str(album.id), "label": "Detail Album", "artist": "A",
            "unit": "count", "count": 12, "time_ms": 1_000_000,
            "first_listen": None, "last_listen": None, "per_year": [],
            "top_tracks": [],
            "top_albums": [{"album_obj": album, "genres": ["Hip-Hop"], "value": 12}],
            "as_of": None, "live_streams": 0,
        }
        _override(app, svc)

        resp = client.get(f"/api/library/stream-history/item?type=album&id={album.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["label"] == "Detail Album" and body["artist"] == "A"
        assert body["top_albums"][0]["album"]["title"] == "Detail Album"
        assert body["top_albums"][0]["album"]["genres"] == ["Hip-Hop"]

    def test_album_not_found_is_404(self, client, app):
        from app.services.library_service import AlbumNotFoundError

        svc = MagicMock()
        svc.stream_history_item_detail.side_effect = AlbumNotFoundError("missing")
        _override(app, svc)

        resp = client.get("/api/library/stream-history/item?type=album&id=missing")
        assert resp.status_code == 404

    def test_bad_type_is_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        resp = client.get("/api/library/stream-history/item?type=playlist&id=x")
        assert resp.status_code == 422
        svc.stream_history_item_detail.assert_not_called()

    def test_missing_id_is_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        resp = client.get("/api/library/stream-history/item?type=track")
        assert resp.status_code == 422


# ── FEAT-analysis-explore: listening clock ──────────────────────────────────────────

class TestStreamHistoryClock:
    def test_clock_cells_and_metric(self, client, app):
        svc = MagicMock()
        svc.stream_history_clock.return_value = {
            "cells": [
                {"weekday": 1, "hour": 9, "plays": 40, "ms_played": 8_000_000},
                {"weekday": 6, "hour": 23, "plays": 12, "ms_played": 2_000_000},
            ],
            "unit": "count", "total_streams": 3252, "total_ms": 628_000_000,
            "as_of": datetime(2026, 6, 21, tzinfo=timezone.utc), "live_streams": 37,
        }
        _override(app, svc)

        resp = client.get("/api/library/stream-history/clock?metric=count")

        assert resp.status_code == 200
        body = resp.json()
        assert body["unit"] == "count" and body["live_streams"] == 37
        assert body["cells"][0] == {"weekday": 1, "hour": 9, "plays": 40, "ms_played": 8_000_000}
        _, kwargs = svc.stream_history_clock.call_args
        assert kwargs == {"user_id": LOCAL_DEV_USER_ID, "metric": "count",
                          "frm": None, "to": None}

    def test_clock_bad_metric_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)
        resp = client.get("/api/library/stream-history/clock?metric=bogus")
        assert resp.status_code == 422
        svc.stream_history_clock.assert_not_called()


# ── FEAT-multi-user Phase 3: library user-scope ─────────────────────────────────────
# Every stream-history read forwards the ACTING member's UUID (JWT sub) as user_id —
# no endpoint may reach the service unscoped. The SQL-side isolation (a member's
# query only sees their own rows) is covered by the real-engine integration tests.

_RANK = {"items": [], "unit": "count", "total_streams": 0, "total_ms": 0, "as_of": None}
_ALBUM_RANK = {**_RANK, "unresolved": 0}
_RETRO = {"per_year": [], "on_this_day": [], "today_kst": "01-01", "as_of": None}
_ITEM = {
    "type": "artist", "id": "X", "label": "X", "artist": None, "unit": "count",
    "count": 0, "time_ms": 0, "first_listen": None, "last_listen": None,
    "per_year": [], "top_tracks": [], "top_albums": [], "as_of": None, "live_streams": 0,
}
_CLOCK = {**_RANK, "cells": []}

_SCOPED_ROUTES = [
    ("stream_history_top_tracks", "/api/library/stream-history/top-tracks", _RANK),
    ("stream_history_top_artists", "/api/library/stream-history/top-artists", _RANK),
    ("stream_history_genre_distribution", "/api/library/stream-history/genre-distribution", _RANK),
    ("stream_history_era_distribution", "/api/library/stream-history/era-distribution", _RANK),
    ("stream_history_top_albums", "/api/library/stream-history/top-albums", _ALBUM_RANK),
    ("stream_history_retrospective", "/api/library/stream-history/retrospective", _RETRO),
    ("stream_history_item_detail", "/api/library/stream-history/item?type=artist&id=X", _ITEM),
    ("stream_history_clock", "/api/library/stream-history/clock", _CLOCK),
]


class TestStreamHistoryMemberScope:
    @pytest.mark.parametrize("method,url,ret", _SCOPED_ROUTES,
                             ids=[m for m, _, _ in _SCOPED_ROUTES])
    def test_member_sub_threads_to_service(self, client, app, method, url, ret):
        # A real member JWT sub (≠ owner, ≠ local dev id) must arrive at the service
        # as user_id — the reviews `_member_id` pattern.
        from app.core.auth import require_cognito_token

        member = uuid.uuid4()
        svc = MagicMock()
        getattr(svc, method).return_value = dict(ret)
        _override(app, svc)
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": str(member)}

        resp = client.get(url)

        assert resp.status_code == 200
        _, kwargs = getattr(svc, method).call_args
        assert kwargs["user_id"] == member
        app.dependency_overrides.clear()
