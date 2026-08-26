"""Real-engine integration tests for RatingService (FEAT-multi-user Phase 1).

Mock unit tests (tests/test_rating_service.py) cover the create-vs-edit + cap
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

from tests.integration.catalog import seed_catalog

from app.services.rating_service import (
    AlbumNotFoundError,
    MemberNotFoundError,
    RatingNotFoundError,
    RatingRateLimitError,
    RatingService,
)

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]


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
        # V50 (FEAT-album-review-authoring Step 1) converging the same table on a
        # branch that may predate it. Written as ALTERs rather than folded into
        # the CREATE above so it converges either way: a branch that already has
        # the table gets the new shape, and a branch that doesn't gets it right
        # after creation. Each DDL is paired with a DROP ... IF EXISTS so a
        # re-run is a no-op instead of a duplicate-object error.
        conn.execute(text("ALTER TABLE album_reviews ALTER COLUMN rating DROP NOT NULL;"))
        conn.execute(text(
            "ALTER TABLE album_reviews "
            "ADD COLUMN IF NOT EXISTS review_candidate BOOLEAN NOT NULL DEFAULT FALSE;"
        ))
        conn.execute(text(
            "ALTER TABLE album_reviews "
            "DROP CONSTRAINT IF EXISTS ck_album_reviews_rating_halfstep;"
        ))
        conn.execute(text("""
            ALTER TABLE album_reviews ADD CONSTRAINT ck_album_reviews_rating_halfstep
              CHECK (rating IS NULL OR (rating >= 0.5 AND rating <= 5.0 AND mod(rating, 0.5) = 0));
        """))
        conn.execute(text(
            "ALTER TABLE album_reviews "
            "DROP CONSTRAINT IF EXISTS ck_album_reviews_comment_needs_rating;"
        ))
        conn.execute(text("""
            ALTER TABLE album_reviews ADD CONSTRAINT ck_album_reviews_comment_needs_rating
              CHECK (comment IS NULL OR rating IS NOT NULL);
        """))
        conn.execute(text(
            "ALTER TABLE album_reviews "
            "DROP CONSTRAINT IF EXISTS ck_album_reviews_state_not_empty;"
        ))
        conn.execute(text("""
            ALTER TABLE album_reviews ADD CONSTRAINT ck_album_reviews_state_not_empty
              CHECK (rating IS NOT NULL OR review_candidate);
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
    return RatingService()


def _member(n: int):
    """A distinct member id + claims whose email derives a unique handle."""
    mid = uuid.uuid4()
    return mid, {"sub": str(mid), "email": f"reviewer{n}-{mid.hex[:6]}@example.com"}


class TestAggregate:
    def test_avg_and_count_over_two_reviewers(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        m2, c2 = _member(2)
        svc.upsert(db, m1, c1, album, {"rating": 4.0, "comment": "great"}, daily_cap=50)
        svc.upsert(db, m2, c2, album, {"rating": 5.0}, daily_cap=50)

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
        svc.upsert(db, m1, c1, album, {"rating": 3.0, "comment": "ok"}, daily_cap=50)
        svc.upsert(db, m1, c1, album, {"rating": 4.5, "comment": "better"}, daily_cap=50)

        avg, count, _ = svc.album_aggregate(db, album)
        assert count == 1          # still one review — edited in place
        assert avg == 4.5

    def test_missing_album_raises(self, db, svc):
        m1, c1 = _member(1)
        with pytest.raises(AlbumNotFoundError):
            svc.upsert(db, m1, c1, uuid.uuid4(), {"rating": 5.0}, daily_cap=50)

    def test_daily_cap_enforced_across_albums(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"rating": 4.0}, daily_cap=1)
        with pytest.raises(RatingRateLimitError):
            svc.upsert(db, m1, c1, album_ids[1], {"rating": 4.0}, daily_cap=1)


class TestProfileAndDelete:
    def test_member_profile_feed(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"rating": 4.0, "comment": "a"}, daily_cap=50)
        svc.upsert(db, m1, c1, album_ids[1], {"rating": 2.5, "comment": "b"}, daily_cap=50)

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
        svc.upsert(db, m1, c1, album_ids[0], {"rating": 4.0}, daily_cap=50)
        svc.upsert(db, m1, c1, album_ids[1], {"rating": 3.0}, daily_cap=50)

        members = svc.list_members(db)
        mine = [(u, n) for u, n in members if u.id == m1]
        assert mine and mine[0][1] == 2

    def test_delete_own_then_gone(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"rating": 4.0}, daily_cap=50)
        svc.delete_own(db, m1, album_ids[0])
        _, count, _ = svc.album_aggregate(db, album_ids[0])
        assert count == 0
        with pytest.raises(RatingNotFoundError):
            svc.delete_own(db, m1, album_ids[0])


class TestPrivateStateStaysPrivate:
    """FEAT-album-review-authoring Step 1 — the regression net for the one real
    risk the V50 widening introduces.

    A state that carries only the private editorial mark lives in the same table
    as the public 평가. The single thing keeping it off other people's screens is
    that every public read filters `rating IS NOT NULL`. Drop that filter from
    any one query and the leak is silent — the row simply appears, with a null
    star, in an album's rating list or on a public profile. So there is one test
    per public read, against a real engine: the ORM-level mock suite cannot see
    a missing WHERE clause.
    """

    def test_mark_only_state_is_absent_from_the_album_aggregate(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album, {"review_candidate": True}, daily_cap=50)

        avg, count, rows = svc.album_aggregate(db, album)
        assert (avg, count, rows) == (None, 0, [])

    def test_a_mark_does_not_skew_a_real_average(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        m2, c2 = _member(2)
        svc.upsert(db, m1, c1, album, {"rating": 4.0}, daily_cap=50)
        svc.upsert(db, m2, c2, album, {"review_candidate": True}, daily_cap=50)

        avg, count, rows = svc.album_aggregate(db, album)
        assert (avg, count) == (4.0, 1)
        assert [u.id for _, u in rows] == [m1]

    def test_mark_only_state_is_absent_from_the_public_profile(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"rating": 4.0}, daily_cap=50)
        svc.upsert(db, m1, c1, album_ids[1], {"review_candidate": True}, daily_cap=50)

        _user, rows = svc.member_profile(db, c1["email"].split("@")[0])
        assert [r.album_id for r, _a in rows] == [album_ids[0]]

    def test_mark_only_state_is_absent_from_the_member_index(self, db, svc, album_ids):
        """list_members feeds the front's static profile prerender. A member who
        has only marked albums has published nothing and gets no profile page."""
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"review_candidate": True}, daily_cap=50)

        assert [(u, n) for u, n in svc.list_members(db) if u.id == m1] == []

    def test_the_author_can_read_their_own_mark(self, db, svc, album_ids):
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"review_candidate": True}, daily_cap=50)
        svc.upsert(db, m1, c1, album_ids[1], {"rating": 3.5}, daily_cap=50)

        states = svc.my_states(db, m1)
        by_album = {s.album_id: s for s in states}
        assert by_album[album_ids[0]].review_candidate is True
        assert by_album[album_ids[0]].rating is None
        assert by_album[album_ids[1]].review_candidate is False

        one = svc.my_states(db, m1, album_ids[0])
        assert [s.album_id for s in one] == [album_ids[0]]

    def test_nobody_elses_states_come_back(self, db, svc, album_ids):
        m1, c1 = _member(1)
        m2, _c2 = _member(2)
        svc.upsert(db, m1, c1, album_ids[0], {"review_candidate": True}, daily_cap=50)

        assert svc.my_states(db, m2) == []


class TestTheReviewCandidateQueue:
    """Step 2 — the harvest read. Against a real engine because the two things
    that can go wrong here are both WHERE-clause shaped: listing an unmarked
    state, or listing someone else's."""

    def test_only_marked_states_are_in_the_queue(self, db, svc, album_ids):
        """A plain 평가 is not a promise to write a 평론. If the filter went
        missing, every rated album would silently become a queue item."""
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"review_candidate": True}, daily_cap=50)
        svc.upsert(db, m1, c1, album_ids[1], {"rating": 3.5}, daily_cap=50)

        assert [r.album_id for r, _a in svc.my_review_candidates(db, m1)] == [album_ids[0]]

    def test_clearing_the_mark_drops_it_from_the_queue(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album, {"rating": 4.0, "review_candidate": True}, daily_cap=50)

        svc.upsert(db, m1, c1, album, {"review_candidate": False}, daily_cap=50)

        assert svc.my_review_candidates(db, m1) == []
        # …and the 평가 it was sitting on is untouched.
        _avg, count, _rows = svc.album_aggregate(db, album)
        assert count == 1

    def test_the_queue_is_per_member(self, db, svc, album_ids):
        m1, c1 = _member(1)
        m2, _c2 = _member(2)
        svc.upsert(db, m1, c1, album_ids[0], {"review_candidate": True}, daily_cap=50)

        assert svc.my_review_candidates(db, m2) == []

    def test_an_unrated_mark_still_carries_its_album(self, db, svc, album_ids):
        """The queue's whole point: an album marked before listening, in no
        bucket, with no rating, still renders. The inner join is what guarantees
        a title is there."""
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"review_candidate": True}, daily_cap=50)

        (state, album), = svc.my_review_candidates(db, m1)
        assert state.rating is None
        assert album.id == album_ids[0]
        assert album.title


class TestStateInvariantsAgainstTheDatabase:
    """The V50 CHECKs, exercised through the service. These are invariants the
    schema enforces — the service is supposed to make them unreachable, and a
    test that only asserted service behaviour would not notice if a CHECK went
    missing from a migration."""

    def test_dropping_the_rating_keeps_a_marked_row(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album, {"rating": 4.0, "comment": "한 줄"}, daily_cap=50)
        svc.upsert(db, m1, c1, album, {"review_candidate": True}, daily_cap=50)

        svc.delete_own(db, m1, album)

        states = svc.my_states(db, m1, album)
        assert len(states) == 1
        assert states[0].rating is None
        assert states[0].comment is None       # the one-liner goes with the star
        assert states[0].review_candidate is True
        _avg, count, _rows = svc.album_aggregate(db, album)
        assert count == 0                       # and nothing public survives

    def test_dropping_the_last_facet_removes_the_row(self, db, svc, album_ids):
        album = album_ids[0]
        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album, {"rating": 4.0}, daily_cap=50)

        state, _user = svc.upsert(db, m1, c1, album, {"rating": None}, daily_cap=50)

        assert state is None
        assert svc.my_states(db, m1, album) == []

    def test_a_one_liner_cannot_exist_without_a_star(self, db, svc, album_ids):
        """ck_album_reviews_comment_needs_rating. Asserted against the DB rather
        than the service so a migration that forgot the CHECK fails here."""
        from sqlalchemy.exc import IntegrityError

        m1, c1 = _member(1)
        svc.upsert(db, m1, c1, album_ids[0], {"review_candidate": True}, daily_cap=50)

        with pytest.raises(IntegrityError):
            db.execute(text(
                "UPDATE album_reviews SET comment = 'orphan' "
                "WHERE user_id = :u AND album_id = :a"
            ), {"u": m1, "a": album_ids[0]})
            db.flush()
        db.rollback()
