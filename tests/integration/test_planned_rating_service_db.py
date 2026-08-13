"""Real-engine integration tests for PlannedRatingService (FEAT-rating-smart-
collections Step 2, Option B).

Mocks cannot prove the new uq_planned_ratings_user_album UNIQUE constraint
actually rejects a duplicate, or that ON CONFLICT DO NOTHING actually makes a
double-mark idempotent instead of raising — both only surface against real
Postgres (feedback-sa-session-lifecycle-mock-blind).

Gated on TEST_DB_URL (Neon test branch). Each test runs inside an outer
transaction rolled back on teardown — nothing persists.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.services.planned_rating_service import PlannedRatingService
from app.services.rating_service import AlbumNotFoundError

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        # Guard against a lagging test branch — additive, idempotent (V52).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS planned_ratings (
              id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
              user_id    UUID        NOT NULL REFERENCES users (id)  ON DELETE CASCADE,
              album_id   UUID        NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              CONSTRAINT uq_planned_ratings_user_album UNIQUE (user_id, album_id)
            );
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_planned_ratings_user_created
              ON planned_ratings (user_id, created_at);
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
    return PlannedRatingService()


def _member(n: int):
    mid = uuid.uuid4()
    return mid, {"sub": str(mid), "email": f"planner{n}-{mid.hex[:6]}@example.com"}


class TestMarkAndUnmark:
    def test_mark_then_list(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.mark(db, m1, c1, album_ids[0])

        rows = svc.list_planned(db, m1)
        assert [a.id for _, a in rows] == [album_ids[0]]

    def test_marking_twice_is_a_no_op_not_an_error(self, db, svc, album_ids):
        """Exercises the ON CONFLICT DO NOTHING path against the real
        uq_planned_ratings_user_album constraint — a mock could not prove this
        doesn't raise IntegrityError."""
        m1, c1 = _member(1)
        svc.mark(db, m1, c1, album_ids[0])
        svc.mark(db, m1, c1, album_ids[0])

        rows = svc.list_planned(db, m1)
        assert len(rows) == 1

    def test_unmarking_something_never_planned_is_a_no_op(self, db, svc, album_ids):
        m1, _c1 = _member(1)
        svc.unmark(db, m1, album_ids[0])  # must not raise
        assert svc.list_planned(db, m1) == []

    def test_mark_then_unmark_then_gone(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.mark(db, m1, c1, album_ids[0])
        svc.unmark(db, m1, album_ids[0])
        assert svc.list_planned(db, m1) == []

    def test_missing_album_raises(self, db, svc):
        m1, c1 = _member(1)
        with pytest.raises(AlbumNotFoundError):
            svc.mark(db, m1, c1, uuid.uuid4())

    def test_the_queue_is_per_member(self, db, svc, album_ids):
        m1, c1 = _member(1)
        m2, _c2 = _member(2)
        svc.mark(db, m1, c1, album_ids[0])

        assert svc.list_planned(db, m2) == []

    def test_planned_and_rated_may_coexist(self, db, svc, album_ids):
        """UX decision 2026-08-13: planning and rating are independent facts —
        no auto-remove-on-rate. Asserted at the storage layer: marking a
        planned rating never touches album_reviews at all."""
        from app.services.rating_service import RatingService

        m1, c1 = _member(1)
        svc.mark(db, m1, c1, album_ids[0])
        RatingService().upsert(db, m1, c1, album_ids[0], {"rating": 4.5}, daily_cap=50)

        assert [a.id for _, a in svc.list_planned(db, m1)] == [album_ids[0]]


class TestConstraintAgainstTheDatabase:
    def test_duplicate_insert_bypassing_the_service_is_rejected(self, db, album_ids):
        """The CONFLICT target the service relies on — asserted directly
        against the DB so a migration that dropped the UNIQUE constraint fails
        here, not just silently double-inserts in prod."""
        m1 = uuid.uuid4()
        db.execute(text(
            "INSERT INTO users (id, handle, display_name) "
            "VALUES (:id, :handle, 'Planner') ON CONFLICT (id) DO NOTHING"
        ), {"id": m1, "handle": f"planner-{m1.hex[:8]}"})
        db.execute(text(
            "INSERT INTO planned_ratings (user_id, album_id) VALUES (:u, :a)"
        ), {"u": m1, "a": album_ids[0]})
        db.flush()

        with pytest.raises(IntegrityError):
            db.execute(text(
                "INSERT INTO planned_ratings (user_id, album_id) VALUES (:u, :a)"
            ), {"u": m1, "a": album_ids[0]})
            db.flush()
        db.rollback()
