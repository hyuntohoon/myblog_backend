"""FEAT-genre-artist-distribution Step 4 — play-events distribution route tests
(mocked LibraryService). Same DistributionResponse shape as the saved-tracks source,
so the front chart is source-agnostic."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.di import get_library_service


def _override(app, svc):
    from app.db.session import get_db

    app.dependency_overrides[get_library_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestPlayEventsDistribution:
    def test_genre_distribution_is_play_weighted(self, client, app):
        svc = MagicMock()
        svc.play_events_genre_distribution.return_value = {
            "items": [("K-Pop", 40), ("Hip-Hop", 25)],
            "unclassified_count": 10,
            "total": 75,
            "uncatalogued": 0,  # play_events.album_id is NOT NULL → always cataloged
            "ungenred": 10,
            "last_synced_at": datetime(2026, 6, 2, tzinfo=timezone.utc),
        }
        _override(app, svc)

        resp = client.get("/api/library/play-events/genre-distribution")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 75
        assert body["unclassified_breakdown"] == {"uncatalogued": 0, "ungenred": 10}
        assert sum(i["count"] for i in body["items"]) + body["unclassified_count"] == body["total"]

    def test_artist_distribution_has_null_breakdown(self, client, app):
        svc = MagicMock()
        svc.play_events_artist_distribution.return_value = {
            "items": [("Artist A", 12)],
            "unclassified_count": 0,
            "total": 12,
            "uncatalogued": None,
            "ungenred": None,
            "last_synced_at": None,
        }
        _override(app, svc)

        resp = client.get("/api/library/play-events/artist-distribution")

        assert resp.status_code == 200
        assert resp.json()["unclassified_breakdown"] is None
