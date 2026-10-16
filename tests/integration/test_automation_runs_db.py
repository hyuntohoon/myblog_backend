"""FEAT-automation-admin 3c — the run-now write path against real Postgres.

What the unit suite cannot see: whether the claim's gates (replay, paused,
cooldown) read committed rows correctly, whether two confirmations racing on two
connections can both pass the cooldown, and whether the SQL is valid against V62.
The service commits its own short transactions, so these tests use committed rows
and clean up after themselves (a per-test actor tag; run rows of the two keys are
cleared before each test, since the cooldown reads every actor's rows).

V62 must already be applied — asserted, never created here.
"""
from __future__ import annotations

import os
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services import automation_catalog as catalog
from app.services.automation_service import AutomationService, ControlConflict, RunRejected, read_db_state

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]

KEY = "youtube_ref_refresh"
OTHER = "isrc_backfill"


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True, pool_size=4)
    yield eng
    eng.dispose()


def _clear(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM automation_control_events WHERE action = 'run' AND job_key IN (:a, :b)"),
                  {"a": KEY, "b": OTHER})
        c.execute(text("DELETE FROM automation_controls WHERE job_key IN (:a, :b)"), {"a": KEY, "b": OTHER})


@pytest.fixture
def actor():
    return "it-" + uuid.uuid4().hex[:12]


class Invoker:
    def __init__(self, accept=True):
        self.accept = accept
        self.calls = []

    def __call__(self, key, rid):
        self.calls.append((key, rid))
        return self.accept


@pytest.fixture
def invoker():
    return Invoker()


@pytest.fixture
def svc(engine, actor, invoker):
    with engine.connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'automation_control_events'"))}
    assert {"invoked_at", "started_at", "finished_at", "run_outcome"} <= cols, "V62 missing"
    _clear(engine)
    yield AutomationService(sessionmaker(bind=engine), invoker=invoker)
    with engine.begin() as c:
        c.execute(text("DELETE FROM automation_control_events WHERE actor_sub = :s"), {"s": actor})
    _clear(engine)


def _row(engine, rid):
    with engine.connect() as c:
        return c.execute(text("SELECT job_key, action, outcome, invoked_at, started_at, actor_sub "
                              "FROM automation_control_events WHERE request_id = :r"), {"r": rid}).one_or_none()


def test_run_records_requested_then_invoked(engine, svc, actor, invoker):
    rid = uuid.uuid4()
    result = svc.run(KEY, rid, actor)
    assert (result.outcome, result.replayed) == ("invoked", False)
    assert invoker.calls == [(KEY, rid)]
    job_key, action, outcome, invoked_at, started_at, who = _row(engine, rid)
    assert (job_key, action, outcome, who) == (KEY, "run", "invoked", actor)
    assert invoked_at is not None and started_at is None
    assert result.run.cooldown_until == result.run.last.requested_at + timedelta(seconds=catalog.RUN_COOLDOWN_SECONDS)


def test_a_second_run_inside_the_cooldown_is_rejected_and_writes_nothing(engine, svc, actor, invoker):
    svc.run(KEY, uuid.uuid4(), actor)
    rid = uuid.uuid4()
    with pytest.raises(RunRejected) as rejected:
        svc.run(KEY, rid, actor)
    assert rejected.value.code == "cooldown"
    assert 0 < rejected.value.retry_after_seconds <= catalog.RUN_COOLDOWN_SECONDS
    assert _row(engine, rid) is None and len(invoker.calls) == 1
    # Per job: another job is not cooled down.
    assert svc.run(OTHER, uuid.uuid4(), actor).outcome == "invoked"


def test_a_failed_invoke_starts_no_cooldown(engine, svc, actor, invoker):
    invoker.accept = False
    first = svc.run(KEY, uuid.uuid4(), actor)
    assert first.outcome == "invoke_failed" and first.run.cooldown_until is None
    assert _row(engine, first.request_id)[3] is None  # no invoked_at
    invoker.accept = True
    assert svc.run(KEY, uuid.uuid4(), actor).outcome == "invoked"


def test_a_failed_invoke_the_worker_started_anyway_counts(engine, svc, actor, invoker):
    # A read timeout on an invoke Lambda had in fact accepted: the worker's start stamp
    # is the evidence, and it must cool the job down.
    invoker.accept = False
    first = svc.run(KEY, uuid.uuid4(), actor)
    with engine.begin() as c:
        c.execute(text("UPDATE automation_control_events SET started_at = clock_timestamp() WHERE request_id = :r"),
                  {"r": first.request_id})
    invoker.accept = True
    with pytest.raises(RunRejected) as rejected:
        svc.run(KEY, uuid.uuid4(), actor)
    assert rejected.value.code == "cooldown"


def test_a_resent_id_replays_and_never_invokes_again(engine, svc, actor, invoker):
    rid = uuid.uuid4()
    svc.run(KEY, rid, actor)
    # Even after the job was paused, the original answer stands and nothing re-runs.
    svc.control(KEY, "pause", uuid.uuid4(), actor)
    replay = svc.run(KEY, rid, actor)
    assert (replay.outcome, replay.replayed) == ("invoked", True)
    assert len(invoker.calls) == 1


def test_a_paused_job_is_rejected_and_writes_nothing(engine, svc, actor, invoker):
    svc.control(KEY, "pause", uuid.uuid4(), actor)
    rid = uuid.uuid4()
    with pytest.raises(RunRejected) as rejected:
        svc.run(KEY, rid, actor)
    assert rejected.value.code == "job_paused"
    assert _row(engine, rid) is None and invoker.calls == []


def test_an_id_used_for_another_job_or_action_is_a_conflict(engine, svc, actor, invoker):
    rid = uuid.uuid4()
    svc.control(OTHER, "pause", rid, actor)
    with pytest.raises(ControlConflict):
        svc.run(KEY, rid, actor)
    rid2 = uuid.uuid4()
    svc.run(KEY, rid2, actor)
    with pytest.raises(ControlConflict):
        svc.run(OTHER, rid2, actor)
    assert invoker.calls == [(KEY, rid2)]


def test_two_racing_confirmations_start_exactly_one_run(engine, svc, actor, invoker):
    # Deterministic, not timing-dependent: each request waits right after its cooldown
    # check until the other has also checked (or 3 s pass). Under the control-row lock
    # the second request cannot reach its check while the first holds the lock, so the
    # first times out, commits, and the second then sees its row. Without the lock both
    # pass the check together and both would start a run.
    from sqlalchemy import event

    meeting = threading.Barrier(2, timeout=3)

    def after_cooldown_check(conn, cursor, statement, params, context, executemany):
        if "SELECT max(created_at), clock_timestamp()" in statement:
            try:
                meeting.wait()
            except threading.BrokenBarrierError:
                pass

    event.listen(engine, "after_cursor_execute", after_cooldown_check)
    start = threading.Barrier(2)
    outcomes = []

    def go():
        start.wait()
        try:
            outcomes.append(svc.run(KEY, uuid.uuid4(), actor).outcome)
        except RunRejected as rejected:
            outcomes.append(rejected.code)

    try:
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        event.remove(engine, "after_cursor_execute", after_cooldown_check)
    assert sorted(outcomes) == ["cooldown", "invoked"]
    assert len(invoker.calls) == 1


def test_snapshot_reads_the_latest_run_and_its_cooldown(engine, svc, actor, invoker):
    invoker.accept = False
    svc.run(KEY, uuid.uuid4(), actor)
    invoker.accept = True
    ok = svc.run(KEY, uuid.uuid4(), actor)
    with sessionmaker(bind=engine)() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        run = read_db_state(db).runs[KEY]
    assert run.request_id == ok.request_id and run.outcome == "invoked"
    assert run.cooldown_from == ok.run.last.requested_at


def test_the_invoke_answer_is_written_once(engine, svc, actor, invoker):
    from app.services.automation_service import _record_invoke

    first = svc.run(KEY, uuid.uuid4(), actor)
    # A late second answer (e.g. a resend racing the first) never rewrites the row.
    with sessionmaker(bind=engine)() as db:
        row, _ = _record_invoke(db, first.request_id, "invoke_failed")
    assert row.outcome == "invoked" and row.invoked_at is not None
    assert _row(engine, first.request_id)[2] == "invoked"
