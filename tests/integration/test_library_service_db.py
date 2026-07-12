"""Real-engine integration tests for LibraryService (FEAT-member-dashboard Step 2).

Mock unit tests (tests/api/test_library.py) cover the route↔service contract but
are blind to SQL semantics: the UNIQUE(album_id) dedup, dense position rewrite on
reorder, and the post_albums⋈posts derived reviewed view. Those only surface
against real Postgres (cf. feedback-sa-session-lifecycle-mock-blind — LibraryService
owns its commit boundary, so one real-engine test is required).

Gated on TEST_DB_URL (Neon test branch; see reference-test-db-url-source). Each
test runs inside an outer transaction rolled back on teardown — nothing persists.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.services.library_service import (
    AlbumNotFoundError,
    DuplicateItemError,
    ItemNotFoundError,
    LibraryService,
)
from myblog_shared_db.models import AlbumToListenItem

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    outer = conn.begin()
    Session = sessionmaker(bind=conn, join_transaction_mode="create_savepoint")
    sess = Session()
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def album_ids(db):
    rows = db.execute(text("SELECT id FROM albums LIMIT 5")).all()
    ids = [str(r[0]) for r in rows]
    if len(ids) < 3:
        pytest.skip("need ≥3 albums in test DB")
    return ids


@pytest.fixture
def svc():
    return LibraryService()


@pytest.fixture
def user_id(db):
    # FEAT-multi-user Phase 2: the FK target for album_to_listen_items.user_id.
    # Inserted inside the test's outer transaction (db.flush, NOT commit) so it
    # rolls back on teardown. Distinct uuid+handle from the bucket file so a
    # combined run can't collide on the unique handle.
    import uuid as _uuid

    uid = _uuid.UUID("00000000-0000-0000-0000-0000000000b2")
    db.execute(
        text(
            "INSERT INTO users (id, handle, display_name) VALUES (:id, :h, :d) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(uid), "h": "test-librarysvc", "d": "Test LibrarySvc"},
    )
    db.flush()
    return uid


class TestToListen:
    def test_add_appends_dense_positions(self, db, svc, album_ids, user_id):
        for aid in album_ids[:3]:
            svc.add_to_listen(db, user_id, album_id=aid)
        items = svc.list_to_listen(db, user_id)
        mine = [i for i in items if str(i.album_id) in album_ids[:3]]
        assert [i.position for i in mine] == [0, 1, 2]

    def test_duplicate_album_blocked(self, db, svc, album_ids, user_id):
        # SAME user_id so the per-user UNIQUE(user_id, album_id) dedup fires.
        svc.add_to_listen(db, user_id, album_id=album_ids[0])
        with pytest.raises(DuplicateItemError):
            svc.add_to_listen(db, user_id, album_id=album_ids[0])
        rows = db.execute(
            select(AlbumToListenItem).where(
                AlbumToListenItem.album_id == album_ids[0]
            )
        ).all()
        assert len(rows) == 1

    def test_missing_album_raises(self, db, svc, user_id):
        with pytest.raises(AlbumNotFoundError):
            svc.add_to_listen(
                db, user_id, album_id="00000000-0000-0000-0000-000000000000"
            )

    def test_reorder_rewrites_positions(self, db, svc, album_ids, user_id):
        items = [svc.add_to_listen(db, user_id, album_id=a) for a in album_ids[:3]]
        new_order = [str(items[2].id), str(items[0].id), str(items[1].id)]
        svc.reorder_to_listen(db, user_id, new_order)
        by_id = {str(i.id): i.position for i in svc.list_to_listen(db, user_id)}
        assert by_id[new_order[0]] == 0
        assert by_id[new_order[1]] == 1
        assert by_id[new_order[2]] == 2

    def test_reorder_unknown_item_raises(self, db, svc, user_id):
        with pytest.raises(ItemNotFoundError):
            svc.reorder_to_listen(
                db, user_id, ["00000000-0000-0000-0000-000000000000"]
            )

    def test_delete_removes_row(self, db, svc, album_ids, user_id):
        item = svc.add_to_listen(db, user_id, album_id=album_ids[0])
        assert svc.delete_to_listen(db, user_id, str(item.id)) is True
        assert svc.delete_to_listen(db, user_id, str(item.id)) is False


class TestRecentlyListened:
    def test_reads_cache_ordered_by_last_played(self, db, svc, album_ids):
        # seed two cache rows (rolled back on teardown via savepoint)
        db.execute(
            text(
                "INSERT INTO spotify_recent_albums (album_id, last_played_at) "
                "VALUES (:a, '2026-06-04T09:00:00Z'), (:b, '2026-06-04T10:00:00Z')"
            ),
            {"a": album_ids[0], "b": album_ids[1]},
        )
        rows = svc.list_recently_listened(db)
        mine = [(str(a.id), lp) for a, lp in rows if str(a.id) in album_ids[:2]]
        # most-recent first → album_ids[1] (10:00) precedes album_ids[0] (09:00)
        assert [aid for aid, _ in mine] == [album_ids[1], album_ids[0]]

    def test_now_playing_none_when_empty(self, db, svc):
        db.execute(text("DELETE FROM spotify_now_playing"))
        assert svc.get_now_playing(db) is None


class TestReviewed:
    def test_reviewed_returns_albums_with_review_ids(self, db, svc):
        rows = svc.list_reviewed(db)
        # Shape contract holds regardless of fixture data volume.
        for album, review_ids in rows:
            assert hasattr(album, "id")
            assert isinstance(review_ids, list) and review_ids
            # every id is a published post for that album
            count = db.execute(
                text(
                    "SELECT count(*) FROM post_albums pa JOIN posts p "
                    "ON p.id = pa.post_id "
                    "WHERE pa.album_id = :aid AND p.status = 'published'"
                ),
                {"aid": str(album.id)},
            ).scalar_one()
            assert count == len(review_ids)


# ── FEAT-member-dashboard-realdata (D-A / D-B) ────────────────────────────────────

def _seed_album(db, spotify_id: str) -> str:
    row = db.execute(
        text("INSERT INTO albums (title, spotify_id) VALUES (:t, :s) RETURNING id"),
        {"t": "ITEST realdata album", "s": spotify_id},
    ).first()
    return str(row[0])


class TestListenedAlbums:
    def test_aggregates_play_events_per_album(self, db, svc):
        """D-A: the cumulative archive is derived from spotify_play_events — one row
        per album with the real play_count + first/last play. Seeded album is
        isolated, so exact counts hold (whole test rolls back)."""
        aid = _seed_album(db, f"itest_la_{uuid.uuid4().hex[:8]}")
        for h in (8, 9, 10):  # three distinct plays
            db.execute(
                text(
                    "INSERT INTO spotify_play_events (album_id, played_at) "
                    "VALUES (:a, :p) ON CONFLICT DO NOTHING"
                ),
                {"a": aid, "p": datetime(2026, 6, 4, h, 0, tzinfo=timezone.utc)},
            )
        rows, total = svc.list_listened_albums(db, limit=500, offset=0)
        mine = [r for r in rows if str(r[0].id) == aid]
        assert len(mine) == 1
        album, play_count, first_played, last_played = mine[0]
        assert play_count == 3
        assert first_played == datetime(2026, 6, 4, 8, 0, tzinfo=timezone.utc)
        assert last_played == datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)
        assert total >= 1  # at least our album in the distinct-album count

    def test_pagination_limits_rows(self, db, svc):
        a1 = _seed_album(db, f"itest_la1_{uuid.uuid4().hex[:8]}")
        a2 = _seed_album(db, f"itest_la2_{uuid.uuid4().hex[:8]}")
        db.execute(text("INSERT INTO spotify_play_events (album_id, played_at) VALUES (:a, :p)"),
                   {"a": a1, "p": datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)})
        db.execute(text("INSERT INTO spotify_play_events (album_id, played_at) VALUES (:a, :p)"),
                   {"a": a2, "p": datetime(2026, 6, 4, 11, 0, tzinfo=timezone.utc)})
        rows, total = svc.list_listened_albums(db, limit=1, offset=0)
        assert len(rows) == 1            # limit honored
        assert total >= 2                # total counts all distinct albums, not the page


class TestRecentTracks:
    def test_orders_newest_first_and_resolves_album(self, db, svc):
        """D-B: recent-tracks are newest-first; a catalog album resolves via the FK
        (.album set), a non-catalog track is still returned with album None."""
        aid = _seed_album(db, f"itest_rtsvc_{uuid.uuid4().hex[:8]}")
        run = uuid.uuid4().hex[:8]
        tid_cat, tid_non = f"itest_rt_cat_{run}", f"itest_rt_non_{run}"
        db.execute(
            text(
                "INSERT INTO spotify_recent_tracks "
                "(spotify_track_id, track_name, artist_name, album_name, album_id, played_at) "
                "VALUES (:t, 'Cat song', 'A', 'Alb', :a, :p)"
            ),
            {"t": tid_cat, "a": aid, "p": datetime(2026, 6, 4, 9, 0, tzinfo=timezone.utc)},
        )
        db.execute(
            text(
                "INSERT INTO spotify_recent_tracks "
                "(spotify_track_id, track_name, artist_name, album_name, album_id, played_at) "
                "VALUES (:t, 'Non song', 'B', 'NonAlb', NULL, :p)"
            ),
            {"t": tid_non, "p": datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)},  # newer
        )
        rows = svc.list_recent_tracks(db)
        mine = [r for r in rows if r.spotify_track_id in (tid_cat, tid_non)]
        # newest first → non-catalog (10:00) precedes catalog (09:00)
        assert [r.spotify_track_id for r in mine] == [tid_non, tid_cat]
        non, cat = mine
        assert non.album_id is None and non.album is None      # non-catalog still returned
        assert str(cat.album_id) == aid and cat.album is not None  # catalog resolves
        assert cat.album.id is not None


# ── FEAT-analysis-explore: time-range over the lifetime+live union ───────────────────
# The [frm, to) window threads through _unified_events into BOTH union legs. Mocks can't
# see the half-open boundary, the KST per-year regroup, or that the clock covers the same
# population as the totals (cf. feedback-sa-session-lifecycle-mock-blind). Seeds use a
# UNIQUE artist so entity-scoped drill-down gives EXACT counts regardless of fixture data;
# all timestamps sit well below the real as_of, so they land in the import leg and never
# shift as_of (the union boundary stays the real import horizon).

def _seed_stream(db, *, ts, uri, artist, ms=200_000, user_id=None):
    # FEAT-multi-user Phase 3 (library user-scope): rows are owned; user_id=None
    # seeds a legacy NULL row (invisible to EVERY member under strict equality).
    db.execute(
        text(
            "INSERT INTO spotify_stream_history "
            "(ts, ms_played, spotify_track_uri, track_name, artist_name, skipped, user_id) "
            "VALUES (:ts, :ms, :uri, 'ITEST track', :artist, false, :uid)"
        ),
        {"ts": ts, "ms": ms, "uri": uri, "artist": artist,
         "uid": str(user_id) if user_id else None},
    )


class TestStreamHistoryRange:
    def test_half_open_range_boundary(self, db, svc, user_id):
        run = uuid.uuid4().hex[:8]
        artist = f"ITEST_RANGE_{run}"
        r1 = datetime(2024, 3, 1, tzinfo=timezone.utc)
        r2 = datetime(2024, 9, 1, tzinfo=timezone.utc)
        r3 = datetime(2025, 3, 1, tzinfo=timezone.utc)
        _seed_stream(db, ts=r1, uri=f"spotify:track:itest_{run}_1", artist=artist, user_id=user_id)
        _seed_stream(db, ts=r2, uri=f"spotify:track:itest_{run}_2", artist=artist, user_id=user_id)
        _seed_stream(db, ts=r3, uri=f"spotify:track:itest_{run}_3", artist=artist, user_id=user_id)

        # whole history → all 3; KST per-year 2024:2 + 2025:1; first/last = the extremes
        full = svc.stream_history_item_detail(db, user_id=user_id, type_="artist", id_=artist)
        assert full["count"] == 3
        assert full["time_ms"] == 600_000
        assert full["first_listen"] == r1 and full["last_listen"] == r3
        assert {y["year"]: y["plays"] for y in full["per_year"]} == {2024: 2, 2025: 1}

        # [2024-01-01, 2025-01-01) → only the two 2024 rows
        y2024 = svc.stream_history_item_detail(
            db, user_id=user_id, type_="artist", id_=artist,
            frm=datetime(2024, 1, 1, tzinfo=timezone.utc),
            to=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        assert y2024["count"] == 2
        assert {y["year"] for y in y2024["per_year"]} == {2024}

        # `to` is EXCLUSIVE — a row exactly at `to` drops out
        excl = svc.stream_history_item_detail(
            db, user_id=user_id, type_="artist", id_=artist,
            frm=datetime(2024, 1, 1, tzinfo=timezone.utc), to=r2,
        )
        assert excl["count"] == 1

        # `from` is INCLUSIVE — a row exactly at `from` stays in
        incl = svc.stream_history_item_detail(
            db, user_id=user_id, type_="artist", id_=artist, frm=r2
        )
        assert incl["count"] == 2  # r2 (at from) + r3

    def test_clock_covers_same_population_as_totals(self, db, svc, user_id):
        # Every event lands in exactly ONE hour×weekday cell, so the clock's play-sum
        # equals total_streams over the SAME range — a union double-count would break this.
        run = uuid.uuid4().hex[:8]
        _seed_stream(db, ts=datetime(2024, 1, 5, 9, 0, tzinfo=timezone.utc),
                     uri=f"spotify:track:itest_{run}_c1", artist=f"ITEST_CLK_{run}",
                     user_id=user_id)
        _seed_stream(db, ts=datetime(2024, 1, 20, 22, 0, tzinfo=timezone.utc),
                     uri=f"spotify:track:itest_{run}_c2", artist=f"ITEST_CLK_{run}",
                     user_id=user_id)
        frm = datetime(2024, 1, 1, tzinfo=timezone.utc)
        to = datetime(2024, 2, 1, tzinfo=timezone.utc)
        clock = svc.stream_history_clock(db, user_id=user_id, frm=frm, to=to)
        assert clock["total_streams"] == 2
        assert sum(c["plays"] for c in clock["cells"]) == clock["total_streams"]
        # cells are valid KST coordinates
        for c in clock["cells"]:
            assert 0 <= c["weekday"] <= 6 and 0 <= c["hour"] <= 23

    def test_window_excluding_seed_is_empty(self, db, svc, user_id):
        run = uuid.uuid4().hex[:8]
        artist = f"ITEST_WIN_{run}"
        _seed_stream(
            db, ts=datetime(2013, 7, 1, 12, 0, tzinfo=timezone.utc),
            uri=f"spotify:track:itest_{run}_a", artist=artist, user_id=user_id,
        )
        # a window entirely before the seed → the entity has zero plays in range
        before = svc.stream_history_item_detail(
            db, user_id=user_id, type_="artist", id_=artist,
            frm=datetime(2012, 1, 1, tzinfo=timezone.utc),
            to=datetime(2013, 1, 1, tzinfo=timezone.utc),
        )
        assert before["count"] == 0
        assert before["first_listen"] is None and before["per_year"] == []
        assert before["top_tracks"] == [] and before["top_albums"] == []

    def test_as_of_invariant_under_range(self, db, svc, user_id):
        # as_of is the requester's import horizon (max ts of THEIR rows), never the
        # ranged subset.
        run = uuid.uuid4().hex[:8]
        _seed_stream(db, ts=datetime(2024, 3, 1, tzinfo=timezone.utc),
                     uri=f"spotify:track:itest_{run}_h1", artist=f"ITEST_ASOF_{run}",
                     user_id=user_id)
        _seed_stream(db, ts=datetime(2025, 3, 1, tzinfo=timezone.utc),
                     uri=f"spotify:track:itest_{run}_h2", artist=f"ITEST_ASOF_{run}",
                     user_id=user_id)
        unranged = svc._stream_totals(db, user_id)
        ranged = svc._stream_totals(
            db, user_id, datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 6, 1, tzinfo=timezone.utc),
        )
        assert unranged["as_of"] == datetime(2025, 3, 1, tzinfo=timezone.utc)
        assert ranged["as_of"] == unranged["as_of"]


# ── FEAT-multi-user Phase 3: library user-scope (V46 user_id) ────────────────────────
# Strict-equality isolation over spotify_stream_history.user_id: a member only ever
# aggregates their OWN imported rows; legacy NULL rows are invisible to everyone.
# (Prod is fully owner-backfilled per V46, so the owner keeps the exact pre-scope
# rowset — asserted here by seeding under one user and reading as another.)

class TestStreamHistoryUserScope:
    @pytest.fixture
    def other_user_id(self, db):
        import uuid as _uuid

        uid = _uuid.UUID("00000000-0000-0000-0000-0000000000b3")
        db.execute(
            text(
                "INSERT INTO users (id, handle, display_name) VALUES (:id, :h, :d) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(uid), "h": "test-librarysvc-b", "d": "Test LibrarySvc B"},
        )
        db.flush()
        return uid

    def test_other_member_sees_zero_rows(self, db, svc, user_id, other_user_id):
        run = uuid.uuid4().hex[:8]
        artist = f"ITEST_SCOPE_{run}"
        ts = datetime(2024, 5, 1, tzinfo=timezone.utc)
        _seed_stream(db, ts=ts, uri=f"spotify:track:itest_{run}_s1", artist=artist,
                     user_id=user_id)

        # the owning user sees their row …
        mine = svc.stream_history_item_detail(db, user_id=user_id, type_="artist", id_=artist)
        assert mine["count"] == 1
        # … a DIFFERENT member sees nothing of it: entity empty, totals 0, as_of None
        theirs = svc.stream_history_item_detail(
            db, user_id=other_user_id, type_="artist", id_=artist
        )
        assert theirs["count"] == 0 and theirs["per_year"] == []
        totals = svc._stream_totals(db, other_user_id)
        assert totals["total_streams"] == 0 and totals["total_ms"] == 0
        assert totals["as_of"] is None and totals["live_streams"] == 0

    def test_null_user_rows_invisible_to_members(self, db, svc, user_id):
        # Legacy/unowned NULL rows never leak into a member's view (strict equality,
        # not IS NOT DISTINCT FROM).
        run = uuid.uuid4().hex[:8]
        artist = f"ITEST_NULL_{run}"
        _seed_stream(db, ts=datetime(2024, 6, 1, tzinfo=timezone.utc),
                     uri=f"spotify:track:itest_{run}_n1", artist=artist, user_id=None)
        got = svc.stream_history_item_detail(db, user_id=user_id, type_="artist", id_=artist)
        assert got["count"] == 0
