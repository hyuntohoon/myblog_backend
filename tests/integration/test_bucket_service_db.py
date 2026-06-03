"""Real-engine integration tests for BucketService.

Mock unit tests (tests/api/test_buckets.py) cover the route↔service contract but
are blind to actual SQL semantics: position renumbering, the UNIQUE(bucket_id,
album_id) guard, the partial single-`is_done` index, ON DELETE CASCADE, and the
post_albums join behind `already_reviewed`. Those only surface against a real
Postgres (cf. feedback-sa-session-lifecycle-mock-blind).

Gated on TEST_DB_URL (Neon test branch; see reference-test-db-url-source). Each
test runs inside an outer transaction rolled back on teardown — nothing persists,
so the shared test branch stays pristine.
"""
from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.services.bucket_service import (
    BucketService,
    DuplicateItemError,
)
from myblog_shared_db.models import (
    Album,
    ReviewBucketItem,
    post_albums_table as post_albums,
)

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
    # Bind the session to a single connection wrapped in an outer transaction.
    # join_transaction_mode="create_savepoint" means the service's internal
    # commit()s release savepoints rather than the outer transaction, so the
    # final rollback wipes everything the test created.
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
    return BucketService()


def _positions(db, bucket_id):
    rows = (
        db.query(ReviewBucketItem.album_id, ReviewBucketItem.position)
        .filter(ReviewBucketItem.bucket_id == bucket_id)
        .order_by(ReviewBucketItem.position)
        .all()
    )
    return [(str(a), p) for a, p in rows]


class TestBucketCrud:
    def test_create_assigns_incrementing_positions(self, db, svc):
        b1 = svc.create_bucket(db, name="꼭")
        b2 = svc.create_bucket(db, name="신보")
        assert b2.position == b1.position + 1

    def test_delete_cascades_items(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="보류")
        svc.add_item(db, str(b.id), album_id=album_ids[0])
        assert svc.delete_bucket(db, str(b.id)) is True
        remaining = db.execute(
            select(ReviewBucketItem).where(ReviewBucketItem.bucket_id == b.id)
        ).all()
        assert remaining == []


class TestAddItem:
    def test_positions_stay_dense_and_ordered_by_score(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="대기")
        for aid in album_ids[:3]:
            svc.add_item(db, str(b.id), album_id=aid)

        persisted = _positions(db, b.id)
        # Dense 0..n regardless of insertion order.
        assert [p for _, p in persisted] == [0, 1, 2]

        # Order must match live recency+popularity score (desc) — the seed rule.
        today = date.today()
        albums = {
            str(a.id): a
            for a in db.query(Album).filter(Album.id.in_(album_ids[:3])).all()
        }
        expected = sorted(
            (aid for aid, _ in persisted),
            key=lambda aid: svc._score(albums[aid], today=today)[0],
            reverse=True,
        )
        assert [aid for aid, _ in persisted] == expected

    def test_duplicate_album_blocked(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="중복")
        svc.add_item(db, str(b.id), album_id=album_ids[0])
        with pytest.raises(DuplicateItemError):
            svc.add_item(db, str(b.id), album_id=album_ids[0])

    def test_rec_reason_recorded(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="이유")
        item = svc.add_item(db, str(b.id), album_id=album_ids[0])
        # rec_reason is 신보/인기/None depending on the album's columns; the row
        # round-trips whatever was computed.
        assert item.rec_reason in ("신보", "인기", None)


class TestReorder:
    def test_intra_bucket_reverse_persists(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="정렬")
        items = [svc.add_item(db, str(b.id), album_id=a) for a in album_ids[:3]]
        rev = [str(items[2].id), str(items[1].id), str(items[0].id)]
        svc.reorder(db, [{"id": str(b.id), "item_ids": rev}])

        persisted = sorted(
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == b.id)
            .all(),
            key=lambda it: it.position,
        )
        assert [str(it.id) for it in persisted] == rev
        assert [it.position for it in persisted] == [0, 1, 2]

    def test_cross_bucket_move(self, db, svc, album_ids):
        b1 = svc.create_bucket(db, name="from")
        b2 = svc.create_bucket(db, name="to")
        i0 = svc.add_item(db, str(b1.id), album_id=album_ids[0])
        i1 = svc.add_item(db, str(b1.id), album_id=album_ids[1])

        # Move i0 into b2; keep i1 in b1.
        svc.reorder(
            db,
            [
                {"id": str(b1.id), "item_ids": [str(i1.id)]},
                {"id": str(b2.id), "item_ids": [str(i0.id)]},
            ],
        )
        db.refresh(i0)
        db.refresh(i1)
        assert str(i0.bucket_id) == str(b2.id)
        assert i0.position == 0
        assert str(i1.bucket_id) == str(b1.id)
        assert i1.position == 0


class TestDoneBucketConstraint:
    def test_at_most_one_done_bucket(self, db, svc):
        b1 = svc.create_bucket(db, name="완료")
        svc.update_bucket(db, str(b1.id), is_done=True)

        b2 = svc.create_bucket(db, name="완료2")
        # The partial unique index must reject a second done bucket.
        with pytest.raises(IntegrityError):
            svc.update_bucket(db, str(b2.id), is_done=True)
        db.rollback()


class TestAlreadyReviewed:
    def test_flags_albums_in_post_albums(self, db, svc, album_ids):
        reviewed_row = db.execute(
            select(post_albums.c.album_id).limit(1)
        ).first()
        reviewed_id = str(reviewed_row[0]) if reviewed_row else None

        # An album that is NOT in post_albums must not be flagged.
        not_reviewed = next(
            (a for a in album_ids if a != reviewed_id), album_ids[0]
        )
        result = svc.reviewed_album_ids(db, [not_reviewed])
        assert not_reviewed not in result

        if reviewed_id:
            assert reviewed_id in svc.reviewed_album_ids(db, [reviewed_id])
