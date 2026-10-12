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
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.integration.catalog import seed_catalog

from app.services import automation_catalog as catalog
from app.services.automation_service import (
    AutomationService,
    read_db_state,
    read_queue_detail,
    resolve_queue,
)

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


# ── 2f: queue detail ────────────────────────────────────────────────────────────


def _detail(db, key):
    target = resolve_queue(key)
    assert target is not None, key
    return read_queue_detail(db, target)


def test_every_detail_sql_runs_and_agrees_with_the_snapshot_states(db):
    """Per queue, the reason breakdown sums to the snapshot's per-state counts.

    The two use separately written state expressions; if they drift, the panel
    would explain a different population than the counts above it. The seeds
    put rows on the boundaries the expressions decide: a job both cancelled and
    complete, and a pending Genius annotation the worker can never pick up.
    """
    cat = seed_catalog(db)
    tag = uuid.uuid4().hex[:10]
    db.execute(
        text("INSERT INTO lyrics_album_jobs (spotify_album_id, cancelled, enumeration_complete, expected_tracks) "
             "VALUES (:a, true, true, 3)"),
        {"a": f"it-{tag}-both"},
    )
    db.execute(
        text("INSERT INTO track_genius_annotations (genius_annotation_id, track_id, fragment, referent_ordinal, "
             "body_source) VALUES (:g, :t, 'it', 0, '')"),
        {"g": 9_000_000_000 + int(tag[:6], 16), "t": cat.track_ids[0]},
    )
    snap = read_db_state(db)
    keys = [qd.key for qd in catalog.QUEUES] + [
        k for k in snap.queues if k.startswith(f"{catalog.TRANSLATION_WORK_TABLE}:")
    ]
    for key in keys:
        read = _detail(db, key)
        summed: dict[str, int] = {}
        for state, _reason, n, _deferred in read.reasons:
            summed[state] = summed.get(state, 0) + int(n)
        assert summed == snap.queues[key].counts, key
        assert len(read.flow) == 2
        assert len(read.oldest) <= catalog.OLDEST_WAITING_LIMIT


def test_reason_text_never_leaves_the_database(db):
    """`code: detail` → code; anything not code-shaped → `unrecognized`."""
    cat = seed_catalog(db)
    version = f"it/{uuid.uuid4()}"
    secret = "token=sk-live-should-never-be-returned"
    rows = [
        (cat.track_ids[0], "a" * 64, "temporary_error"),
        (cat.track_ids[1], "b" * 64, f"engine_validation: {secret}"),
        (cat.track_ids[2], "c" * 64, f"Traceback (most recent call last): {secret}"),
    ]
    for track, fp, reason in rows:
        db.execute(
            text("INSERT INTO lyrics_translation_work (track_id, source_fingerprint, lang, translator_version, "
                 "status, attempts, last_reason, next_attempt_at, created_at) "
                 "VALUES (:t, :f, 'ko', :v, 'retryable_error', 1, :r, now() + interval '1 hour', "
                 "clock_timestamp())"),
            {"t": track, "f": fp, "v": version, "r": reason},
        )
    # A row of a neighbouring version must not leak into this one.
    db.execute(
        text("INSERT INTO lyrics_translation_work (track_id, source_fingerprint, lang, translator_version) "
             "VALUES (:t, :f, 'ko', :v)"),
        {"t": cat.track_ids[0], "f": "d" * 64, "v": version + "-other"},
    )

    read = _detail(db, catalog.translation_queue_key(version))

    assert sorted(read.reasons) == [
        ("retryable_error", "engine_validation", 1, 1),
        ("retryable_error", "temporary_error", 1, 1),
        ("retryable_error", catalog.UNRECOGNIZED_REASON, 1, 1),
    ]
    assert read.flow == (3, 0)
    assert [r[3] for r in read.oldest] == ["temporary_error", "engine_validation", catalog.UNRECOGNIZED_REASON]
    assert all(r[4] == 1 for r in read.oldest)
    assert "sk-live" not in repr(read)
    assert "Traceback" not in repr(read)


def test_album_job_detail_titles_oldest_first_and_backoff(db):
    cat = seed_catalog(db)
    tag = uuid.uuid4().hex[:10]
    db.execute(
        text("INSERT INTO lyrics_album_jobs (spotify_album_id, album_id, last_reason, next_attempt_at, created_at) "
             "VALUES (:a, :album, :ra, now() + interval '1 hour', now() - interval '3650 days'), "
             "       (:b, NULL, :rb, now() - interval '1 hour', now() - interval '3649 days')"),
        {"a": f"it-{tag}-a", "b": f"it-{tag}-b", "album": cat.album_ids[0],
         "ra": f"it_{tag}_ahead", "rb": f"it_{tag}_due"},
    )
    read = _detail(db, "lyrics_album_jobs")

    # Seeded ten years back, so they are the two oldest on any target.
    first, second = read.oldest[0], read.oldest[1]
    assert first[0] == f"it-{tag}-a" and first[1] is not None and first[3] == f"it_{tag}_ahead"
    assert second[0] == f"it-{tag}-b" and second[1] is None and second[3] == f"it_{tag}_due"
    # Backing off = next_attempt_at still ahead; the overdue one is not deferred.
    by = {(s, r): (n, d) for s, r, n, d in read.reasons}
    assert by[("pending", f"it_{tag}_ahead")] == (1, 1)
    assert by[("pending", f"it_{tag}_due")] == (1, 0)
    assert first[6] > read.observed_at and second[6] < read.observed_at


def test_oldest_filters_order_and_flow_definitions(db):
    """Which rows count as waiting, in what order, and what moves the flow.

    Each seed sits on the far side of a filter: a finished research row, an
    ineligible Genius annotation, a GPT publish that nobody requested. Deltas
    only — the target may already hold rows.
    """
    cat = seed_catalog(db)
    before = {k: _detail(db, k) for k in
              ("album_research", "genre_backfill_requests", "track_genius_annotations", "track_lyrics_translations")}

    # album_research: one queued from long ago, one finished just now.
    queued_id, done_id = (
        db.execute(
            text("INSERT INTO album_research (album_id, prompt_version, status, requested_at, started_at, finished_at) "
                 "VALUES (:a, :p, :s, :req, :st, :fin) RETURNING id"),
            {"a": cat.album_ids[i], "p": f"it-{uuid.uuid4()}", "s": s, "req": req, "st": req if fin else None, "fin": fin},
        ).scalar_one()
        for i, s, req, fin in [
            (0, "queued", _ago(3650), None),
            (1, "done", _ago(3649), _ago(0)),
        ]
    )
    # genre_backfill_requests: two pending, inserted newest first.
    newer = db.execute(text("INSERT INTO genre_backfill_requests (requested_at) VALUES (now() - interval '3649 days') "
                            "RETURNING id")).scalar_one()
    older = db.execute(text("INSERT INTO genre_backfill_requests (requested_at) VALUES (now() - interval '3650 days') "
                            "RETURNING id")).scalar_one()
    # track_genius_annotations: pending but ineligible (no source body) — never waiting.
    ineligible = 9_100_000_000 + int(uuid.uuid4().hex[:6], 16)
    db.execute(
        text("INSERT INTO track_genius_annotations (genius_annotation_id, track_id, fragment, referent_ordinal, "
             "body_source, created_at) VALUES (:g, :t, 'it', 0, '', now() - interval '3650 days')"),
        {"g": ineligible, "t": cat.track_ids[0]},
    )
    # track_lyrics_translations: a GPT publish nobody requested (requested_at =
    # translated_at, as _publish writes it) and a real request.
    db.execute(
        text("INSERT INTO track_lyrics_translations (track_id, status, segments, source_fingerprint, origin, "
             "translated_at) VALUES (:t, 'done', '[]'::jsonb, :fp, 'poller', now())"),
        {"t": cat.track_ids[1], "fp": "e" * 64},
    )
    db.execute(text("INSERT INTO track_lyrics_translations (track_id, status) VALUES (:t, 'requested')"),
               {"t": cat.track_ids[2]})

    research = _detail(db, "album_research")
    assert research.oldest[0][0] == str(queued_id)
    assert str(done_id) not in [r[0] for r in research.oldest]
    assert research.flow[1] - before["album_research"].flow[1] == 1  # finished_at, not requested_at
    assert research.flow[0] == before["album_research"].flow[0]

    genre = _detail(db, "genre_backfill_requests")
    assert [r[0] for r in genre.oldest[:2]] == [str(older), str(newer)]

    genius = _detail(db, "track_genius_annotations")
    assert str(ineligible) not in [r[0] for r in genius.oldest]

    tlt = _detail(db, "track_lyrics_translations")
    assert tlt.flow[0] - before["track_lyrics_translations"].flow[0] == 1  # the request, not the publish
    assert tlt.flow[1] is None
    assert cat.track_ids[2] in [r[0] for r in tlt.oldest] or len(tlt.oldest) == catalog.OLDEST_WAITING_LIMIT


def _ago(days: int):
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_detail_runs_inside_a_read_only_service_transaction(engine):
    Session = sessionmaker(bind=engine)
    d = AutomationService(Session).queue_detail("lyrics_album_tracks")
    assert d is not None and d.status == "ok"
    assert d.flow.entered is None  # the table has no created_at
    with engine.connect() as conn:
        assert conn.execute(text("SHOW transaction_read_only")).scalar() == "off"
