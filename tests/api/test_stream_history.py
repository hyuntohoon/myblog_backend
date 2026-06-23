"""FEAT-listening-history-import Step 4 — lifetime stream-history ranking routes
(mocked LibraryService). Ungated count/time top tracks/artists over the imported
Spotify Extended Streaming History; the `unit` field names the Count↔Time axis."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.di import get_library_service


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
        # default metric=count, default limit=15
        _, kwargs = svc.stream_history_top_tracks.call_args
        assert kwargs == {"metric": "count", "limit": 15}

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
        assert kwargs == {"metric": "time", "limit": 5}

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
        assert kwargs == {"metric": "count"}

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
