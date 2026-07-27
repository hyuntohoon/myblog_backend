"""Real-engine integration tests for BucketService.grow_nightly.

The mock tests (tests/api/test_nightly_grow.py) pin the route↔service contract but
are blind to the SQL that matters here: the ownership JOIN (owner's buckets only),
the prep_tonight / post_id-IS-NULL filters, and the multi-bucket same-album case
(an album checked in two of the owner's buckets is stamped in both). Those only
surface against a real Postgres (cf. feedback-sa-session-lifecycle-mock-blind).

Gated on TEST_DB_URL (Neon test branch); self-skips when unset, same harness as
test_bucket_service_db.py — every test runs inside an outer transaction rolled
back on teardown, so the shared branch stays pristine.
"""
from __future__ import annotations

import os
import uuid as _uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.bucket_service import (
    BucketService,
    GrowPostNotDraftError,
    GrowPostNotFoundError,
)
from myblog_shared_db.models import ReviewBucketItem

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]


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
def svc():
    return BucketService()


@pytest.fixture
def album_ids(db):
    rows = db.execute(text("SELECT id FROM albums LIMIT 3")).all()
    ids = [str(r[0]) for r in rows]
    if len(ids) < 2:
        pytest.skip("need ≥2 albums in test DB")
    return ids


def _mk_user(db, uid: _uuid.UUID, handle: str) -> _uuid.UUID:
    db.execute(
        text(
            "INSERT INTO users (id, handle, display_name) VALUES (:id, :h, :d) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(uid), "h": handle, "d": handle},
    )
    db.flush()
    return uid


@pytest.fixture
def owner_id(db):
    # Distinct uuid+handle from the other integration files (unique handle).
    return _mk_user(db, _uuid.UUID("00000000-0000-0000-0000-0000000000c1"), "test-growsvc-owner")


@pytest.fixture
def foreign_id(db):
    return _mk_user(db, _uuid.UUID("00000000-0000-0000-0000-0000000000c2"), "test-growsvc-member")


def _mk_draft(db, slug_suffix: str, status: str = "draft") -> _uuid.UUID:
    row = db.execute(
        text(
            "INSERT INTO posts (slug, title, posted_date, status) "
            "VALUES (:slug, :title, CURRENT_DATE, :status) RETURNING id"
        ),
        {"slug": f"grow-test-{slug_suffix}", "title": "grow test", "status": status},
    ).first()
    db.flush()
    return row[0]


def _checked_item(db, svc, user_id, album_id: str, bucket_name: str):
    """Owner-path seeding through the service API (no raw column guessing)."""
    bucket = svc.create_bucket(db, user_id, name=bucket_name)
    item = svc.add_item(db, user_id, str(bucket.id), album_id=album_id)
    svc.update_item(db, user_id, str(bucket.id), str(item.id), prep_tonight=True)
    return bucket, item


def _reload(db, item_id) -> ReviewBucketItem:
    db.expire_all()
    return db.get(ReviewBucketItem, item_id)


class TestGrowNightly:
    def test_stamps_and_unchecks_every_owner_item_for_the_album(
        self, db, svc, owner_id, album_ids
    ):
        # The same album checked in TWO of the owner's buckets = one writing
        # subject, two membership rows — both must be stamped in one call.
        _, it1 = _checked_item(db, svc, owner_id, album_ids[0], "grow-a")
        _, it2 = _checked_item(db, svc, owner_id, album_ids[0], "grow-b")
        post_id = _mk_draft(db, "stamp")

        grown = svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), post_id)

        assert grown == 2
        for it in (it1, it2):
            row = _reload(db, it.id)
            assert row.prep_tonight is False
            assert row.post_id == post_id

    def test_idempotent_second_call_returns_zero(self, db, svc, owner_id, album_ids):
        _checked_item(db, svc, owner_id, album_ids[0], "grow-idem")
        post_id = _mk_draft(db, "idem")

        assert svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), post_id) == 1
        assert svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), post_id) == 0

    def test_never_touches_a_foreign_users_items(
        self, db, svc, owner_id, foreign_id, album_ids
    ):
        # A member checked the same album in THEIR bucket. The owner-pinned grow
        # must not stamp it — that row is exactly what Phase B is for.
        _, foreign_item = _checked_item(db, svc, foreign_id, album_ids[0], "grow-f")
        _, owner_item = _checked_item(db, svc, owner_id, album_ids[0], "grow-o")
        post_id = _mk_draft(db, "foreign")

        grown = svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), post_id)

        assert grown == 1
        assert _reload(db, owner_item.id).post_id == post_id
        f = _reload(db, foreign_item.id)
        assert f.prep_tonight is True
        assert f.post_id is None

    def test_other_albums_are_untouched(self, db, svc, owner_id, album_ids):
        _, other = _checked_item(db, svc, owner_id, album_ids[1], "grow-other")
        post_id = _mk_draft(db, "scope")

        grown = svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), post_id)

        assert grown == 0
        o = _reload(db, other.id)
        assert o.prep_tonight is True and o.post_id is None

    def test_existing_post_id_is_never_overwritten(self, db, svc, owner_id, album_ids):
        bucket, item = _checked_item(db, svc, owner_id, album_ids[0], "grow-keep")
        first = _mk_draft(db, "keep-1")
        svc.update_item(db, owner_id, str(bucket.id), str(item.id), post_id=str(first))
        second = _mk_draft(db, "keep-2")

        grown = svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), second)

        assert grown == 0
        assert _reload(db, item.id).post_id == first

    def test_missing_post_raises(self, db, svc, owner_id, album_ids):
        _checked_item(db, svc, owner_id, album_ids[0], "grow-404")
        with pytest.raises(GrowPostNotFoundError):
            svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), _uuid.uuid4())

    def test_non_draft_post_raises(self, db, svc, owner_id, album_ids):
        _checked_item(db, svc, owner_id, album_ids[0], "grow-409")
        published = _mk_draft(db, "published", status="published")
        with pytest.raises(GrowPostNotDraftError):
            svc.grow_nightly(db, str(owner_id), _uuid.UUID(album_ids[0]), published)

    def test_empty_owner_sub_matches_nothing(self, db, svc, owner_id, album_ids):
        # local/dev has no configured owner — grow must be a no-op, never a guess.
        _checked_item(db, svc, owner_id, album_ids[0], "grow-empty")
        post_id = _mk_draft(db, "empty")
        assert svc.grow_nightly(db, "", _uuid.UUID(album_ids[0]), post_id) == 0
