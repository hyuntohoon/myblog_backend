"""FEAT-genre-artist-distribution Step 3/5 — saved-tracks route tests (mocked
LibraryService, the established backend pattern). Pure counting/resolution logic is
covered in tests/test_distribution.py."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from app.di import get_library_service, get_sqs_client


def _album(album_id="alb-1", title="Album", artists=("Artist A",)):
    a = MagicMock()
    a.id = album_id
    a.title = title
    a.cover_url = "https://cdn/c.jpg"
    a.release_date = date(2026, 5, 1)
    a.popularity = 70
    a.artists = [MagicMock() for _ in artists]
    for m, n in zip(a.artists, artists):
        m.name = n
    return a


def _saved_row(tid="t1", album_id=None, album=None):
    r = MagicMock()
    r.spotify_track_id = tid
    r.track_name = "Song"
    r.artist_name = "Artist A"
    r.album_name = "Album"
    r.album_sid = "spalb1"
    r.album_id = album_id
    r.album = album
    r.added_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return r


def _override(app, svc, sqs=None):
    from app.db.session import get_db

    app.dependency_overrides[get_library_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()
    if sqs is not None:
        app.dependency_overrides[get_sqs_client] = lambda: sqs


class TestSavedTracksList:
    def test_list_maps_rows_with_album_brief(self, client, app):
        svc = MagicMock()
        svc.list_saved_tracks.return_value = (
            [_saved_row(album_id="alb-1", album=_album())],
            1,
            datetime(2026, 6, 2, tzinfo=timezone.utc),
        )
        _override(app, svc)

        resp = client.get("/api/library/saved-tracks")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["spotify_track_id"] == "t1"
        assert body["items"][0]["album"]["id"] == "alb-1"

    def test_list_populates_album_genres(self, client, app, monkeypatch):
        # The /profile 분석 버킷 facet/chip groups by the album's primary genre, so the
        # list must fill album.genres via GenreService.labels_map (schema-identical —
        # no contract change). Patch the resolver so the route's batch call returns labels.
        svc = MagicMock()
        svc.list_saved_tracks.return_value = (
            [_saved_row(album_id="alb-1", album=_album())],
            1,
            None,
        )
        _override(app, svc)
        fake_genre_svc = MagicMock()
        fake_genre_svc.labels_map.return_value = {"alb-1": ["Ambient", "Jazz"]}
        monkeypatch.setattr("app.api.routes.library.GenreService", lambda: fake_genre_svc)

        resp = client.get("/api/library/saved-tracks")

        assert resp.status_code == 200
        assert resp.json()["items"][0]["album"]["genres"] == ["Ambient", "Jazz"]

    def test_list_album_null_when_not_in_catalog(self, client, app):
        svc = MagicMock()
        svc.list_saved_tracks.return_value = ([_saved_row(album_id=None, album=None)], 1, None)
        _override(app, svc)

        resp = client.get("/api/library/saved-tracks")

        assert resp.json()["items"][0]["album"] is None
        assert resp.json()["items"][0]["album_id"] is None


class TestSavedTracksDistribution:
    def test_genre_distribution_shape_and_partition(self, client, app):
        svc = MagicMock()
        svc.saved_tracks_genre_distribution.return_value = {
            "items": [("K-Pop", 5), ("Hip-Hop", 3)],
            "unclassified_count": 2,
            "total": 10,
            "uncatalogued": 1,
            "ungenred": 1,
            "last_synced_at": datetime(2026, 6, 2, tzinfo=timezone.utc),
        }
        _override(app, svc)

        resp = client.get("/api/library/saved-tracks/genre-distribution")

        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == [
            {"label": "K-Pop", "count": 5},
            {"label": "Hip-Hop", "count": 3},
        ]
        assert body["unclassified_count"] == 2
        assert body["total"] == 10
        assert body["unclassified_breakdown"] == {"uncatalogued": 1, "ungenred": 1}
        # the chart's stated denominator holds: sum(count) + unclassified == total
        assert sum(i["count"] for i in body["items"]) + body["unclassified_count"] == body["total"]

    def test_artist_distribution_has_null_breakdown(self, client, app):
        svc = MagicMock()
        svc.saved_tracks_artist_distribution.return_value = {
            "items": [("Artist A", 4)],
            "unclassified_count": 0,
            "total": 4,
            "uncatalogued": None,
            "ungenred": None,
            "last_synced_at": None,
        }
        _override(app, svc)

        resp = client.get("/api/library/saved-tracks/artist-distribution")

        assert resp.status_code == 200
        assert resp.json()["unclassified_breakdown"] is None


class TestClassify:
    def test_classify_enqueues_uncatalogued_and_reports_backfill(self, client, app):
        svc = MagicMock()
        svc.unclassified_saved_album_targets.return_value = (["spalb1", "spalb2"], 3)
        sqs = MagicMock()
        sqs.send_album_sync.return_value = 2
        _override(app, svc, sqs)

        resp = client.post("/api/library/saved-tracks/classify")

        assert resp.status_code == 202
        body = resp.json()
        assert body["enqueued"] == 2
        assert body["skipped_needs_backfill"] == 3
        sqs.send_album_sync.assert_called_once_with(["spalb1", "spalb2"])

    def test_classify_no_targets_does_not_enqueue(self, client, app):
        svc = MagicMock()
        svc.unclassified_saved_album_targets.return_value = ([], 0)
        sqs = MagicMock()
        _override(app, svc, sqs)

        resp = client.post("/api/library/saved-tracks/classify")

        assert resp.status_code == 202
        assert resp.json()["enqueued"] == 0
        sqs.send_album_sync.assert_not_called()


class TestFillGenres:
    def _db(self, app, rowcount):
        from app.db.session import get_db
        db = MagicMock()
        db.execute.return_value.rowcount = rowcount
        app.dependency_overrides[get_db] = lambda: db
        return db

    def test_fill_genres_queues(self, client, app):
        self._db(app, 1)  # INSERT ... WHERE NOT EXISTS inserted a row

        resp = client.post("/api/library/saved-tracks/fill-genres")

        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"

    def test_fill_genres_dedup_already_pending(self, client, app):
        self._db(app, 0)  # a pending request already existed → no insert

        resp = client.post("/api/library/saved-tracks/fill-genres")

        assert resp.status_code == 202
        assert resp.json()["status"] == "already_pending"
