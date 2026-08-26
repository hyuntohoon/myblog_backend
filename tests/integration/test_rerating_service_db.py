"""Real-engine integration tests for ReratingService (FEAT-album-rerating).

Everything load-bearing here is a SQL-level fact a mock cannot see
(feedback-sa-session-lifecycle-mock-blind):

  - starting a 재평가 must leave `album_reviews` in a state the V50 CHECKs
    actually permit — the row is DELETED when nothing private remains, and
    survives with a NULL star when `review_candidate` does. A mock would happily
    accept the illegal in-between.
  - the snapshot insert and the rating strip must land in ONE transaction. The
    whole point of `previous_rating NOT NULL` is that a star is never gone with
    nothing left to restore it from, and only a real engine proves the two
    statements commit together.
  - completion is a DELETE issued from RatingService.upsert's transaction. That
    single delete is what clears BOTH surfaces (profile 재평가 중, 마이버킷 다시
    들어볼 앨범), so a regression here silently strands albums in two lists.

Gated on TEST_DB_URL (Neon test branch). Each test runs inside an outer
transaction rolled back on teardown — nothing persists.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.integration.catalog import seed_catalog

from app.services.rating_service import AlbumNotFoundError, RatingService
from app.services.rerating_service import NoRatingToRerateError, ReratingService

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]

DAILY_CAP = 100


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        # Guard against a lagging test branch — additive, idempotent (V54).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pending_reratings (
              id               UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
              user_id          UUID          NOT NULL REFERENCES users (id)  ON DELETE CASCADE,
              album_id         UUID          NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
              previous_rating  NUMERIC(2, 1) NOT NULL,
              previous_comment TEXT,
              created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
              CONSTRAINT uq_pending_reratings_user_album UNIQUE (user_id, album_id),
              CONSTRAINT ck_pending_reratings_previous_rating_halfstep
                CHECK (previous_rating >= 0.5 AND previous_rating <= 5.0
                       AND mod(previous_rating, 0.5) = 0)
            );
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_pending_reratings_user_created
              ON pending_reratings (user_id, created_at);
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
    # OPS-integration-db-locality Step 1 — seeded into this test's transaction
    # instead of borrowed from ambient rows. UUID objects, as before.
    return [uuid.UUID(a) for a in seed_catalog(db).album_ids]


@pytest.fixture
def svc():
    return ReratingService()


@pytest.fixture
def ratings():
    return RatingService()


def _member(n: int):
    mid = uuid.uuid4()
    return mid, {"sub": str(mid), "email": f"rerater{n}-{mid.hex[:6]}@example.com"}


def _state_row(db, user_id, album_id):
    return db.execute(
        text(
            "SELECT rating, comment, review_candidate FROM album_reviews "
            "WHERE user_id = :u AND album_id = :a"
        ),
        {"u": user_id, "a": album_id},
    ).first()


class TestStart:
    def test_withdraws_the_rating_and_snapshots_it(self, db, svc, ratings, album_ids):
        m, c = _member(1)
        ratings.upsert(db, m, c, album_ids[0], {"rating": 3.5, "comment": "좋았다"}, daily_cap=DAILY_CAP)

        pending = svc.start(db, m, c, album_ids[0])

        # The public 평가 is gone — the whole row, since nothing private remained.
        assert _state_row(db, m, album_ids[0]) is None
        assert float(pending.previous_rating) == 3.5
        assert pending.previous_comment == "좋았다"

    def test_row_survives_when_a_private_mark_remains(self, db, svc, ratings, album_ids):
        """The V50 CHECK permits a rating-less row ONLY while review_candidate is
        set. Starting a 재평가 on such an album must null the star in place rather
        than delete the row — otherwise the member's 평론 쓸 것 mark disappears
        with it."""
        m, c = _member(2)
        ratings.upsert(
            db, m, c, album_ids[0],
            {"rating": 4.0, "comment": "hm", "review_candidate": True},
            daily_cap=DAILY_CAP,
        )

        svc.start(db, m, c, album_ids[0])

        row = _state_row(db, m, album_ids[0])
        assert row is not None
        assert row.rating is None
        assert row.comment is None
        assert row.review_candidate is True

    def test_without_a_rating_is_a_conflict(self, db, svc, album_ids):
        m, c = _member(3)
        with pytest.raises(NoRatingToRerateError):
            svc.start(db, m, c, album_ids[0])

    def test_a_mark_alone_is_not_a_rating(self, db, svc, ratings, album_ids):
        """A state that holds only 평론 쓸 것 has no 평가 to withdraw — 409, not a
        rerating opened against a NULL star that could never be restored."""
        m, c = _member(4)
        ratings.upsert(db, m, c, album_ids[0], {"review_candidate": True}, daily_cap=DAILY_CAP)

        with pytest.raises(NoRatingToRerateError):
            svc.start(db, m, c, album_ids[0])

    def test_starting_twice_keeps_the_first_snapshot(self, db, svc, ratings, album_ids):
        """Idempotent, and specifically must NOT re-snapshot: by the second call
        the live rating is already gone, so a naive re-read would overwrite a
        real score with nothing."""
        m, c = _member(5)
        ratings.upsert(db, m, c, album_ids[0], {"rating": 2.5}, daily_cap=DAILY_CAP)

        first = svc.start(db, m, c, album_ids[0])
        again = svc.start(db, m, c, album_ids[0])

        assert again.id == first.id
        assert float(again.previous_rating) == 2.5
        assert len(svc.list_pending(db, m)) == 1

    def test_missing_album_raises(self, db, svc):
        m, c = _member(6)
        with pytest.raises(AlbumNotFoundError):
            svc.start(db, m, c, uuid.uuid4())


class TestCancel:
    def test_restores_the_withdrawn_rating(self, db, svc, ratings, album_ids):
        m, c = _member(7)
        ratings.upsert(db, m, c, album_ids[0], {"rating": 4.5, "comment": "다시 듣자"}, daily_cap=DAILY_CAP)
        svc.start(db, m, c, album_ids[0])

        svc.cancel(db, m, album_ids[0])

        row = _state_row(db, m, album_ids[0])
        assert row is not None
        assert float(row.rating) == 4.5
        assert row.comment == "다시 듣자"
        assert svc.list_pending(db, m) == []

    def test_restores_onto_a_surviving_marked_row(self, db, svc, ratings, album_ids):
        m, c = _member(8)
        ratings.upsert(
            db, m, c, album_ids[0],
            {"rating": 1.5, "review_candidate": True},
            daily_cap=DAILY_CAP,
        )
        svc.start(db, m, c, album_ids[0])

        svc.cancel(db, m, album_ids[0])

        row = _state_row(db, m, album_ids[0])
        assert float(row.rating) == 1.5
        # The private mark was never this call's to touch.
        assert row.review_candidate is True

    def test_cancelling_nothing_is_a_no_op(self, db, svc, album_ids):
        m, _c = _member(9)
        svc.cancel(db, m, album_ids[0])  # must not raise
        assert svc.list_pending(db, m) == []


class TestCompletion:
    def test_a_new_rating_ends_the_rerating(self, db, svc, ratings, album_ids):
        """The single behaviour both surfaces depend on: saving a star deletes the
        pending row, so the profile 재평가 중 section and the 마이버킷 tile clear
        themselves with no per-surface cleanup call."""
        m, c = _member(10)
        ratings.upsert(db, m, c, album_ids[0], {"rating": 3.0}, daily_cap=DAILY_CAP)
        svc.start(db, m, c, album_ids[0])
        assert len(svc.list_pending(db, m)) == 1

        ratings.upsert(db, m, c, album_ids[0], {"rating": 5.0, "comment": "달라졌다"}, daily_cap=DAILY_CAP)

        assert svc.list_pending(db, m) == []
        row = _state_row(db, m, album_ids[0])
        assert float(row.rating) == 5.0

    def test_flipping_only_the_mark_does_not_end_it(self, db, svc, ratings, album_ids):
        """평론 쓸 것 is an editorial concept with nothing to say about a 재평가.
        The guard is on the FINAL rating, so a mark-only write must leave the
        재평가 open — and must not resurrect a star either."""
        m, c = _member(11)
        ratings.upsert(db, m, c, album_ids[0], {"rating": 3.0}, daily_cap=DAILY_CAP)
        svc.start(db, m, c, album_ids[0])

        ratings.upsert(db, m, c, album_ids[0], {"review_candidate": True}, daily_cap=DAILY_CAP)

        assert len(svc.list_pending(db, m)) == 1
        assert _state_row(db, m, album_ids[0]).rating is None

    def test_clearing_a_star_is_not_completing(self, db, svc, ratings, album_ids):
        """A 재평가 that is somehow open on a rated album (cancel raced a rate)
        must not be ended by a write that CLEARS the rating — only a landed star
        completes one."""
        m, c = _member(12)
        ratings.upsert(db, m, c, album_ids[0], {"rating": 3.0}, daily_cap=DAILY_CAP)
        svc.start(db, m, c, album_ids[0])
        ratings.upsert(db, m, c, album_ids[0], {"rating": 2.0}, daily_cap=DAILY_CAP)  # ends it
        svc.start(db, m, c, album_ids[0])  # opened again

        ratings.upsert(db, m, c, album_ids[0], {"rating": None}, daily_cap=DAILY_CAP)

        assert len(svc.list_pending(db, m)) == 1


class TestConstraintsAndScoping:
    def test_duplicate_pending_row_is_rejected_by_the_db(self, db, svc, ratings, album_ids):
        """The idempotent path is service-level; this proves the constraint behind
        it is real, so a concurrent start cannot open two 재평가 for one album."""
        m, c = _member(13)
        ratings.upsert(db, m, c, album_ids[0], {"rating": 3.0}, daily_cap=DAILY_CAP)
        svc.start(db, m, c, album_ids[0])

        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO pending_reratings (user_id, album_id, previous_rating) "
                    "VALUES (:u, :a, 3.0)"
                ),
                {"u": m, "a": album_ids[0]},
            )
        db.rollback()

    def test_half_step_check_rejects_an_unrestorable_snapshot(self, db, album_ids):
        """previous_rating must satisfy the same scale album_reviews holds live
        ratings to — a snapshot that could not be restored is a dead end."""
        m, _c = _member(14)
        db.execute(
            text(
                "INSERT INTO users (id, handle, display_name) "
                "VALUES (:u, :h, :h) ON CONFLICT DO NOTHING"
            ),
            {"u": m, "h": f"rr{m.hex[:8]}"},
        )
        with pytest.raises((IntegrityError, DataError)):
            db.execute(
                text(
                    "INSERT INTO pending_reratings (user_id, album_id, previous_rating) "
                    "VALUES (:u, :a, 3.3)"
                ),
                {"u": m, "a": album_ids[0]},
            )
        db.rollback()

    def test_list_is_scoped_to_one_member(self, db, svc, ratings, album_ids):
        m1, c1 = _member(15)
        m2, c2 = _member(16)
        ratings.upsert(db, m1, c1, album_ids[0], {"rating": 3.0}, daily_cap=DAILY_CAP)
        ratings.upsert(db, m2, c2, album_ids[1], {"rating": 4.0}, daily_cap=DAILY_CAP)
        svc.start(db, m1, c1, album_ids[0])
        svc.start(db, m2, c2, album_ids[1])

        assert [a.id for _, a in svc.list_pending(db, m1)] == [album_ids[0]]
        assert [a.id for _, a in svc.list_pending(db, m2)] == [album_ids[1]]
