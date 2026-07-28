from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.kst import kst_today
from app.core.auth import require_cognito_token
from app.db.session import get_db
from app.di import get_user_service

MEMBER_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
EMPTY_MEMBER_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
TRACKED_ARTIST_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
UNTRACKED_ARTIST_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
DAY0_ALBUM_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


@pytest.fixture
def release_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE artists (id CHAR(32) PRIMARY KEY, name TEXT NOT NULL)")
        )
        conn.execute(
            text(
                "CREATE TABLE user_artist_tracks ("
                "user_id CHAR(32) NOT NULL, artist_id CHAR(32) NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE artist_release_events ("
                "id CHAR(32) PRIMARY KEY, artist_id CHAR(32) NOT NULL, "
                "source TEXT NOT NULL, "
                "source_key TEXT NOT NULL, spotify_album_id TEXT, title TEXT NOT NULL, "
                "release_type TEXT, release_date DATE NOT NULL, status TEXT NOT NULL, "
                "first_seen_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        # Minimal catalog-album mirror for the cover/overlay enrichment join
        # (the route selects only these three columns).
        conn.execute(
            text(
                "CREATE TABLE albums ("
                "id CHAR(32) PRIMARY KEY, spotify_id TEXT, cover_url TEXT)"
            )
        )

    with Session(engine) as db:
        # Must match the route's day-boundary source (KST wall-clock) — seeding
        # relative to the DB's UTC current_date would shift the fixture by one
        # day during 15:00–24:00 UTC and flake these tests.
        today = kst_today()
        db.execute(
            text("INSERT INTO artists (id, name) VALUES (:id, :name)"),
            [
                {"id": TRACKED_ARTIST_ID.hex, "name": "Tracked Artist"},
                {"id": UNTRACKED_ARTIST_ID.hex, "name": "Untracked Artist"},
            ],
        )
        db.execute(
            text(
                "INSERT INTO user_artist_tracks (user_id, artist_id) "
                "VALUES (:user_id, :artist_id)"
            ),
            {"user_id": MEMBER_ID.hex, "artist_id": TRACKED_ARTIST_ID.hex},
        )
        # Only the Day0 spotify album exists in the catalog — the other
        # spotify-bearing event ("spotify-ep") must stay bare (no cover).
        db.execute(
            text(
                "INSERT INTO albums (id, spotify_id, cover_url) "
                "VALUES (:id, :spotify_id, :cover_url)"
            ),
            {
                "id": DAY0_ALBUM_ID.hex,
                "spotify_id": "spotify-day0",
                "cover_url": "https://img.example/day0.jpg",
            },
        )

        events = [
            (TRACKED_ARTIST_ID, "Future Album", "album", 10, "announced", None, "itunes"),
            (TRACKED_ARTIST_ID, "Future EP", "ep", 20, "announced", "spotify-ep", "itunes"),
            (TRACKED_ARTIST_ID, "Recent Single", "single", -2, "released", None, "itunes"),
            (TRACKED_ARTIST_ID, "Recent EP", "ep", -3, "announced", None, "itunes"),
            (TRACKED_ARTIST_ID, "Too Old", "album", -31, "released", None, "itunes"),
            # Release DAY itself: released → recent (must not vanish on day 0),
            # still-announced → upcoming.
            (TRACKED_ARTIST_ID, "Day0 Released", "album", 0, "released", "spotify-day0", "itunes"),
            (TRACKED_ARTIST_ID, "Day0 Announced", "album", 0, "announced", None, "musicbrainz"),
            # One real release observed by two sources → one display item
            # (soft-group on artist/date/normalized title; MB title preferred,
            # release_type coalesced from the iTunes row).
            (TRACKED_ARTIST_ID, "Dupe Album", None, 5, "announced", None, "musicbrainz"),
            (TRACKED_ARTIST_ID, "Dupe Album - Single", "single", 5, "announced", None, "itunes"),
            (UNTRACKED_ARTIST_ID, "Untracked Future", "single", 1, "announced", None, "itunes"),
            (UNTRACKED_ARTIST_ID, "Untracked Recent", "album", -1, "released", None, "itunes"),
        ]
        db.execute(
            text(
                "INSERT INTO artist_release_events ("
                "id, artist_id, source, source_key, spotify_album_id, title, "
                "release_type, release_date, status, first_seen_at, updated_at"
                ") VALUES ("
                ":id, :artist_id, :source, :source_key, :spotify_album_id, :title, "
                ":release_type, :release_date, :status, '2026-01-01', '2026-01-01')"
            ),
            [
                {
                    "id": uuid.uuid4().hex,
                    "artist_id": artist_id.hex,
                    "source": source,
                    "source_key": f"event-{index}",
                    "spotify_album_id": spotify_album_id,
                    "title": title,
                    "release_type": release_type,
                    "release_date": (today + timedelta(days=day_offset)).isoformat(),
                    "status": status,
                }
                for index, (
                    artist_id,
                    title,
                    release_type,
                    day_offset,
                    status,
                    spotify_album_id,
                    source,
                ) in enumerate(events)
            ],
        )
        yield db
    engine.dispose()


def _wire_member(app, db, member_id):
    user_service = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_service] = lambda: user_service
    app.dependency_overrides[require_cognito_token] = lambda: {"sub": str(member_id)}
    return user_service


def test_feed_contains_only_the_members_tracked_artists(app, client, release_db):
    user_service = _wire_member(app, release_db, MEMBER_ID)

    response = client.get("/api/me/release-feed")

    assert response.status_code == 200
    body = response.json()
    assert [item["title"] for item in body["upcoming"]] == [
        "Day0 Announced",
        "Dupe Album",
        "Future Album",
        "Future EP",
    ]
    assert [item["title"] for item in body["recent"]] == [
        "Day0 Released",
        "Recent Single",
        "Recent EP",
    ]
    assert all(
        item["artist_id"] == str(TRACKED_ARTIST_ID)
        for item in body["upcoming"] + body["recent"]
    )
    assert all(
        item["artist_name"] == "Tracked Artist"
        for item in body["upcoming"] + body["recent"]
    )
    assert not any(
        item["title"].startswith("Untracked")
        for item in body["upcoming"] + body["recent"]
    )
    assert [item["trust"] for item in body["upcoming"]] == [
        "예정",
        "예정",
        "예정",
        "확정",
    ]
    assert [item["trust"] for item in body["recent"]] == ["확정", "확정", "불확실"]
    assert user_service.get_or_create.call_args[0][1] == MEMBER_ID


def test_catalog_album_enrichment_attaches_cover_and_album_id(app, client, release_db):
    """An item whose spotify_album_id exists in the catalog carries album_id +
    cover_url; spotify-bearing items without a catalog row, and announced-only
    items, stay bare (FEAT-for-you-releases Step 1)."""
    _wire_member(app, release_db, MEMBER_ID)

    body = client.get("/api/me/release-feed").json()
    by_title = {i["title"]: i for i in body["upcoming"] + body["recent"]}

    day0 = by_title["Day0 Released"]
    assert day0["album_id"] == str(DAY0_ALBUM_ID)
    assert day0["cover_url"] == "https://img.example/day0.jpg"

    # In-catalog miss: spotify id known but no catalog album row.
    assert by_title["Future EP"]["album_id"] is None
    assert by_title["Future EP"]["cover_url"] is None
    # Announced-only: no spotify id at all.
    assert by_title["Recent EP"]["album_id"] is None
    assert by_title["Recent EP"]["cover_url"] is None


def test_feed_is_empty_when_member_tracks_no_artists(app, client, release_db):
    _wire_member(app, release_db, EMPTY_MEMBER_ID)

    response = client.get("/api/me/release-feed")

    assert response.status_code == 200
    assert response.json() == {"upcoming": [], "recent": []}


def test_album_and_upcoming_filters_populate_only_upcoming(app, client, release_db):
    _wire_member(app, release_db, MEMBER_ID)

    response = client.get(
        "/api/me/release-feed", params={"state": "upcoming", "category": "album"}
    )

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["upcoming"]] == [
        "Day0 Announced",
        "Future Album",
        "Future EP",
    ]
    assert response.json()["recent"] == []


def test_single_and_recent_filters_populate_only_recent(app, client, release_db):
    _wire_member(app, release_db, MEMBER_ID)

    response = client.get(
        "/api/me/release-feed", params={"state": "recent", "category": "single"}
    )

    assert response.status_code == 200
    assert response.json()["upcoming"] == []
    assert [item["title"] for item in response.json()["recent"]] == ["Recent Single"]


def test_release_day_tiles_completely(app, client, release_db):
    """A release on its own release day must appear exactly once: released →
    recent (regression: it used to match neither bucket), announced → upcoming."""
    _wire_member(app, release_db, MEMBER_ID)

    body = client.get("/api/me/release-feed").json()

    all_titles = [i["title"] for i in body["upcoming"] + body["recent"]]
    assert all_titles.count("Day0 Released") == 1
    assert all_titles.count("Day0 Announced") == 1
    assert "Day0 Released" in [i["title"] for i in body["recent"]]
    assert "Day0 Announced" in [i["title"] for i in body["upcoming"]]


def test_cross_source_observations_soft_group_to_one_item(app, client, release_db):
    """MB 'Dupe Album' + iTunes 'Dupe Album - Single' (same artist, same day)
    are one release → one item, MB title preferred, type coalesced from iTunes."""
    _wire_member(app, release_db, MEMBER_ID)

    body = client.get("/api/me/release-feed").json()

    dupes = [
        i
        for i in body["upcoming"] + body["recent"]
        if i["title"].startswith("Dupe Album")
    ]
    assert len(dupes) == 1
    assert dupes[0]["title"] == "Dupe Album"
    assert dupes[0]["release_type"] == "single"
    assert dupes[0]["trust"] == "예정"

    # And the coalesced type routes the whole group in category views: it is a
    # single, so it must appear under category=single and not category=album.
    single_titles = [
        i["title"]
        for i in client.get(
            "/api/me/release-feed", params={"category": "single"}
        ).json()["upcoming"]
    ]
    assert "Dupe Album" in single_titles
