"""Real-engine integration tests for ReviewService (FEAT-multi-user Phase 1).

Mock unit tests (tests/test_review_service.py) cover the create-vs-edit + cap
branching but are blind to SQL semantics: the live avg/count aggregate, the
review⋈users and review⋈albums joins, and the list_members group-by. Those only
surface against real Postgres.

Gated on TEST_DB_URL (Neon test branch; see reference-test-db-url-source). Each
test runs inside an outer transaction rolled back on teardown — nothing persists.
The module fixture idempotently ensures users (V36) + album_reviews (V38) exist
so a lagging test branch (reference-neon-test-branch-migration-drift) can't skew
the run.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.review_service import (
    AlbumNotFoundError,
    MemberNotFoundError,
    ReviewNotFoundError,
    ReviewRateLimitError,
    ReviewService,
)

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    # Guard against a lagging test branch — additive, idempotent.
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
              id UUID PRIMARY KEY, email TEXT, handle TEXT NOT NULL,
              display_name TEXT NOT NULL, avatar_url TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              CONSTRAINT uq_users_handle UNIQUE (handle)
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS album_reviews (
              id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
              rating NUMERIC(2,1) NOT NULL, comment TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              CONSTRAINT uq_album_reviews_user_album UNIQUE (user_id, album_id),
              CONSTRAINT ck_album_reviews_rating_halfstep
                CHECK (rating >= 0.5 AND rating <= 5.0 AND mod(rating, 0.5) = 0)
            );
        """))
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
    rows = db.execute(text("SELECT id FROM albums LIMIT 2")).all()
    ids = [r[0] for r in rows]
    if len(ids) < 2:
        pytest.skip("need ≥2 albums in test DB")
    return ids


@pytest.fixture
def svc():
    return ReviewService()


def _member(n: int):
    """A distinct member id + claims whose email derives a unique handle."""
    mid = uuid.uuid4()
    return mid, {"sub": str(mid), "email": f"reviewer{n}-{mid.hex[:6]}@example.com"}


class TestAggregate:
    def test_avg_and_count_over_two_reviewers(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        m2, c2 = _member(2)
        svc.upsert(db, m1, c1, album, 4.0, "great", daily_cap=50)
        svc.upsert(db, m2, c2, album, 5.0, None, daily_cap=50)

        avg, count, rows = svc.album_aggregate(db, album)
        assert count == 2
        assert avg == 4.5
        assert len(rows) == 2
        # newest-first, each carries its author
        assert {u.id for _, u in rows} == {m1, m2}

    def test_empty_album_is_zero(self, db, svc, album_ids):
        avg, count, rows = svc.album_aggregate(db, album_ids[1])
        assert (avg, count, rows) == (None, 0, [])


class TestUpsert:
    def test_edit_updates_rating_not_count(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album, 3.0, "ok", daily_cap=50)
        svc.upsert(db, m1, c1, album, 4.5, "better on relisten", daily_cap=50)

        avg, count, _ = svc.album_aggregate(db, album)
        assert count == 1          # still one review — edited in place
        assert avg == 4.5

    def test_missing_album_raises(self, db, svc):
        m1, c1 = _member(1)
        with pytest.raises(AlbumNotFoundError):
            svc.upsert(db, m1, c1, uuid.uuid4(), 5.0, None, daily_cap=50)

    def test_daily_cap_enforced_across_albums(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], 4.0, None, daily_cap=1)
        with pytest.raises(ReviewRateLimitError):
            svc.upsert(db, m1, c1, album_ids[1], 4.0, None, daily_cap=1)


class TestProfileAndDelete:
    def test_member_profile_feed(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], 4.0, "a", daily_cap=50)
        svc.upsert(db, m1, c1, album_ids[1], 2.5, "b", daily_cap=50)

        user, rows = svc.member_profile(db, c1["email"].split("@")[0])
        assert user.id == m1
        assert len(rows) == 2
        # each row is (review, album) with the album joined in
        assert all(a.id == r.album_id for r, a in rows)

    def test_unknown_handle_raises(self, db, svc):
        with pytest.raises(MemberNotFoundError):
            svc.member_profile(db, "no-such-handle-xyz")

    def test_list_members_counts_reviews(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], 4.0, None, daily_cap=50)
        svc.upsert(db, m1, c1, album_ids[1], 3.0, None, daily_cap=50)

        members = svc.list_members(db)
        mine = [(u, n) for u, n in members if u.id == m1]
        assert mine and mine[0][1] == 2

    def test_delete_own_then_gone(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], 4.0, None, daily_cap=50)
        svc.delete_own(db, m1, album_ids[0])
        _, count, _ = svc.album_aggregate(db, album_ids[0])
        assert count == 0
        with pytest.raises(ReviewNotFoundError):
            svc.delete_own(db, m1, album_ids[0])
