"""Real-engine integration tests for LibraryService.

Mock unit tests (tests/api/test_library.py) cover the route↔service contract but
are blind to actual SQL semantics: the UNIQUE(album_id) upsert (set_status must
update, never insert a second row), the FK to albums, and commit/refresh round-
trips. Those only surface against a real Postgres (cf.
feedback-sa-session-lifecycle-mock-blind — LibraryService introduces a new
commit boundary, so one real-engine test is required).

Gated on TEST_DB_URL (Neon test branch; see reference-test-db-url-source). Each
test runs inside an outer transaction rolled back on teardown — nothing persists.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.services.library_service import AlbumNotFoundError, LibraryService
from myblog_shared_db.models import LibraryItem

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
    if len(ids) < 2:
        pytest.skip("need ≥2 albums in test DB")
    return ids


@pytest.fixture
def svc():
    return LibraryService()


class TestSetStatus:
    def test_create_then_list(self, db, svc, album_ids):
        item, created = svc.set_status(db, album_ids[0], status="wishlist")
        assert created is True
        assert item.status == "wishlist"

        listed = svc.list_items(db)
        assert any(str(i.album_id) == album_ids[0] for i in listed)

    def test_set_status_is_upsert_not_duplicate(self, db, svc, album_ids):
        aid = album_ids[0]
        svc.set_status(db, aid, status="listening")
        item, created = svc.set_status(db, aid, status="reviewed")

        assert created is False
        assert item.status == "reviewed"
        # UNIQUE(album_id): exactly one row for the album.
        rows = db.execute(
            select(LibraryItem).where(LibraryItem.album_id == aid)
        ).all()
        assert len(rows) == 1

    def test_missing_album_raises(self, db, svc):
        with pytest.raises(AlbumNotFoundError):
            svc.set_status(
                db, "00000000-0000-0000-0000-000000000000", status="wishlist"
            )


class TestDelete:
    def test_delete_removes_row(self, db, svc, album_ids):
        aid = album_ids[0]
        svc.set_status(db, aid, status="listened")
        assert svc.delete_item(db, aid) is True
        rows = db.execute(
            select(LibraryItem).where(LibraryItem.album_id == aid)
        ).all()
        assert rows == []

    def test_delete_absent_returns_false(self, db, svc, album_ids):
        assert svc.delete_item(db, album_ids[1]) is False
