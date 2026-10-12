"""FEAT-automation-admin 2b — the automation snapshot's SQL against real Postgres.

The unit suite proves the folding and the session shape with a fake session,
which cannot see whether the catalog's SQL is valid, whether the tables exist,
or whether `SET TRANSACTION READ ONLY` actually holds on a pooled connection.
This file does, against whatever TEST_DB_URL names: the Neon test branch
locally, postgres:16 loaded with shared_db's canonical schema in CI.

It creates no schema (V59 must already be applied — asserted, not created) and
commits nothing: seeds go into the per-test transaction that teardown rolls
back. The target may already hold rows, so assertions are on DELTAS.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.integration.catalog import seed_catalog

from app.services import automation_catalog as catalog
from app.services.automation_service import AutomationService, read_db_state

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
    session = Session()
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


def test_every_read_table_exists(db):
    tables = [q.table for q in catalog.QUEUES] + [catalog.TRANSLATION_WORK_TABLE, "automation_heartbeats"]
    missing = [t for t in tables if db.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is None]
    assert not missing, f"not deployed on this branch: {missing}"


def test_empty_heartbeat_read_and_real_sql(db):
    read = read_db_state(db)
    assert read.observed_at is not None
    for q in catalog.QUEUES:
        assert q.key in read.queues


def test_seeded_rows_move_the_counts(db):
    cat = seed_catalog(db)
    before = read_db_state(db)

    db.execute(
        text("INSERT INTO automation_heartbeats (agent_key, enabled, last_outcome, details) "
             "VALUES ('gpt-translate-poller', true, 'saved 3', '{\"model\": \"x\"}'::jsonb) "
             "ON CONFLICT (agent_key) DO UPDATE SET observed_at = now(), last_outcome = 'saved 3'")
    )
    db.execute(text("INSERT INTO genre_backfill_requests (source) VALUES ('integration-test')"))
    db.execute(
        text("INSERT INTO album_research (album_id, prompt_version, status) VALUES (:a, :p, 'queued')"),
        {"a": cat.album_ids[0], "p": f"it-{uuid.uuid4()}"},
    )
    version = f"it/{uuid.uuid4()}"
    db.execute(
        text("INSERT INTO lyrics_translation_work (track_id, source_fingerprint, lang, translator_version) "
             "VALUES (:t, :f, 'ko', :v)"),
        {"t": cat.track_ids[0], "f": "a" * 64, "v": version},
    )

    after = read_db_state(db)

    def delta(key, state):
        return after.queues[key].counts.get(state, 0) - before.queues[key].counts.get(state, 0)

    assert delta("genre_backfill_requests", "pending") == 1
    assert delta("album_research", "queued") == 1
    assert after.queues["album_research"].oldest_waiting_at is not None
    tw = after.queues[catalog.translation_queue_key(version)]
    assert tw.counts == {"ready": 1}
    assert tw.oldest_waiting_at is not None
    hb = {h.agent_key: h for h in after.heartbeats}["gpt-translate-poller"]
    assert hb.last_outcome == "saved 3"
    assert hb.details == {"model": "x"}
    # observed_at is the DB's now() in both: same transaction, same instant.
    assert hb.observed_at == after.observed_at


def test_service_transaction_is_read_only_on_a_real_connection(engine, monkeypatch):
    """The real session path: SET TRANSACTION READ ONLY must actually bind.

    Swap the reader for one that tries to write. Postgres must refuse it inside
    the service's transaction, and the service must degrade to "unavailable"
    rather than raise — the proof that opening the page cannot write.
    """
    import app.services.automation_service as svc_module

    Session = sessionmaker(bind=engine)
    ok = AutomationService(Session).snapshot()
    assert {s.key: s.status for s in ok.sources}["db"] == "ok"

    attempted = []

    def writing_reader(db):
        attempted.append(True)
        db.execute(text("INSERT INTO genre_backfill_requests (source) VALUES ('must-fail')"))
        raise AssertionError("the write was accepted")

    monkeypatch.setattr(svc_module, "read_db_state", writing_reader)
    snap = AutomationService(Session).snapshot()
    assert attempted == [True]
    assert {s.key: s.status for s in snap.sources}["db"] == "unavailable"

    # The read-only mode did not leak to the next transaction on the pool.
    with engine.connect() as conn:
        assert conn.execute(text("SHOW transaction_read_only")).scalar() == "off"
