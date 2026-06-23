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
