"""Real-Postgres coverage for the today's-pick queue: conflict SQL + promote
atomicity. Mock units can't see pool/transaction semantics (a promote is a
single-commit two-write transaction), so the boundary is exercised here
against the Neon test branch."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.kst import kst_today
from app.services.todays_pick_service import TodaysPickService

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]

# The Neon test branch can lag prod migrations — apply the V48 DDL
# idempotently before the module runs (reference-neon-test-branch-migration-drift).
_V48_DDL = (
    """
    CREATE TABLE IF NOT EXISTS daily_pick_queue (
      id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
      track_id         UUID        NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
      album_id         UUID        NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
      title            TEXT        NOT NULL,
      artist           TEXT        NOT NULL,
      cover_url        TEXT,
      spotify_track_id TEXT        NOT NULL,
      created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_daily_pick_queue_track UNIQUE (track_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_daily_pick_queue_created_at
      ON daily_pick_queue (created_at DESC)
    """,
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        for ddl in _V48_DDL:
            conn.execute(text(ddl))
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    outer = conn.begin()
    Session = sessionmaker(bind=conn, join_transaction_mode="create_savepoint")
    session = Session()
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def tracks(db):
    """Two catalog (track_id, album_id) pairs — the queue FKs need real rows."""
    rows = db.execute(
        text(
            "SELECT t.id, t.album_id FROM tracks t "
            "WHERE t.album_id IS NOT NULL LIMIT 2"
        )
    ).all()
    if len(rows) < 2:
        pytest.skip("need >=2 tracks with albums in test DB")
    return rows


def _add(svc, db, track_id, album_id, *, title="Queued"):
    return svc.add_to_queue(
        db,
        track_id=str(track_id),
        album_id=str(album_id),
        title=title,
        artist="Test Artist",
        cover_url=None,
        spotify_track_id="spotify-test-id",
    )


def _todays_pick_track_id(db):
    """Read back today's pick row by the KST day the service writes.

    `current_date` here would resolve against the session timezone (UTC on Neon),
    so between 00:00 and 09:00 KST it looks up the previous day and finds nothing
    — the A-4 boundary the service already avoids via `app.core.kst`. The date is
    still bound explicitly rather than routed through `get_today()` so the assert
    stays independent of the service's own read path.
    """
    return db.execute(
        text("SELECT track_id FROM daily_picks WHERE pick_date = :day"),
        {"day": kst_today()},
    ).scalar_one_or_none()


def test_readd_hits_unique_and_returns_existing_row(db, tracks):
    svc = TodaysPickService()
    first = _add(svc, db, tracks[0][0], tracks[0][1])
    again = _add(svc, db, tracks[0][0], tracks[0][1], title="Changed")

    assert again.id == first.id
    assert again.title == "Queued"  # DO NOTHING — the original row wins
    assert len(svc.list_queue(db)) == 1


def test_list_queue_orders_newest_first(db, tracks):
    svc = TodaysPickService()
    older = _add(svc, db, tracks[0][0], tracks[0][1])
    newer = _add(svc, db, tracks[1][0], tracks[1][1])
    # In one transaction now() is frozen — separate the timestamps explicitly.
    db.execute(
        text(
            "UPDATE daily_pick_queue SET created_at = created_at - interval '1 hour' "
            "WHERE id = :id"
        ),
        {"id": str(older.id)},
    )
    db.commit()

    assert [row.id for row in svc.list_queue(db)] == [newer.id, older.id]


def test_promote_upserts_pick_and_consumes_queue_row(db, tracks):
    svc = TodaysPickService()
    queued = _add(svc, db, tracks[0][0], tracks[0][1])

    pick = svc.promote_from_queue(db, str(queued.id))

    assert pick is not None
    assert pick.track_id == queued.track_id
    assert _todays_pick_track_id(db) == queued.track_id
    assert svc.list_queue(db) == []
    # The row is consumed — a second promote of the same id is a miss (404 path).
    assert svc.promote_from_queue(db, str(queued.id)) is None


def test_promote_failure_rolls_back_pick_and_keeps_queue_row(db, tracks, monkeypatch):
    svc = TodaysPickService()
    queued = _add(svc, db, tracks[0][0], tracks[0][1])
    pick_before = _todays_pick_track_id(db)

    def _boom(_row):
        raise RuntimeError("forced mid-promote failure")

    monkeypatch.setattr(db, "delete", _boom)
    with pytest.raises(RuntimeError):
        svc.promote_from_queue(db, str(queued.id))
    monkeypatch.undo()

    # One transaction: the pick upsert must have rolled back with the failure,
    # and the queue row must not have been consumed.
    assert _todays_pick_track_id(db) == pick_before
    assert [row.id for row in svc.list_queue(db)] == [queued.id]
