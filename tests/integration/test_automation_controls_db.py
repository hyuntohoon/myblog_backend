"""FEAT-automation-admin 3b — the control write path against real Postgres.

The unit suite cannot see what this file exists for: whether the transaction
order holds under two real connections. The service commits its own short
transaction, so these tests use committed rows and clean up after themselves
(a per-test actor tag for the audit rows; the control rows are reset).

V60 must already be applied — asserted, never created here.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.automation_service import AutomationService, ControlConflict, read_db_state

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


@pytest.fixture
def actor():
    return "it-" + uuid.uuid4().hex[:12]


@pytest.fixture
def svc(engine, actor):
    with engine.begin() as c:
        for t in ("automation_controls", "automation_control_events"):
            assert c.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None, f"V60 missing: {t}"
        c.execute(text("DELETE FROM automation_controls WHERE job_key IN (:a, :b)"), {"a": KEY, "b": OTHER})
    yield AutomationService(sessionmaker(bind=engine))
    with engine.begin() as c:
        c.execute(text("DELETE FROM automation_control_events WHERE actor_sub = :s"), {"s": actor})
        c.execute(text("DELETE FROM automation_controls WHERE job_key IN (:a, :b)"), {"a": KEY, "b": OTHER})


def _flag(engine, key=KEY):
    with engine.connect() as c:
        return c.execute(text("SELECT paused, changed_at, honored_at FROM automation_controls WHERE job_key = :k"),
                         {"k": key}).one_or_none()


def _events(engine, actor):
    with engine.connect() as c:
        return c.execute(text("SELECT request_id, job_key, action, outcome FROM automation_control_events "
                              "WHERE actor_sub = :s ORDER BY created_at"), {"s": actor}).all()


def test_pause_then_pause_again_is_no_change_and_keeps_changed_at(engine, svc, actor):
    first = svc.control(KEY, "pause", uuid.uuid4(), actor)
    assert (first.outcome, first.replayed, first.control.paused) == ("applied", False, True)
    changed = _flag(engine).changed_at

    second = svc.control(KEY, "pause", uuid.uuid4(), actor)
    assert (second.outcome, second.replayed, second.control.paused) == ("no_change", False, True)
    assert _flag(engine).changed_at == changed
    assert [e.outcome for e in _events(engine, actor)] == ["applied", "no_change"]


def test_first_ever_resume_creates_a_running_row(engine, svc, actor):
    r = svc.control(KEY, "resume", uuid.uuid4(), actor)
    assert (r.outcome, r.control.paused) == ("no_change", False)
    assert _flag(engine).paused is False


def test_replay_returns_stored_outcome_with_current_state_and_never_acts_again(engine, svc, actor):
    pause_id = uuid.uuid4()
    svc.control(KEY, "pause", pause_id, actor)
    svc.control(KEY, "resume", uuid.uuid4(), actor)

    replay = svc.control(KEY, "pause", pause_id, actor)
    assert (replay.replayed, replay.outcome) == (True, "applied")
    # An old pause replayed after a resume must not pause again.
    assert replay.control.paused is False and _flag(engine).paused is False
    assert len(_events(engine, actor)) == 2


def test_reused_request_id_for_another_action_or_job_is_a_conflict(engine, svc, actor):
    rid = uuid.uuid4()
    svc.control(KEY, "pause", rid, actor)
    with pytest.raises(ControlConflict):
        svc.control(KEY, "resume", rid, actor)
    with pytest.raises(ControlConflict):
        svc.control(OTHER, "pause", rid, actor)
    assert _flag(engine).paused is True
    # The conflicting request rolled back everything, including the row it upserted.
    assert _flag(engine, OTHER) is None
    assert len(_events(engine, actor)) == 1


def _race(fn, n=2):
    barrier, out, errors = threading.Barrier(n), [None] * n, [None] * n

    def run(i):
        barrier.wait()
        try:
            out[i] = fn(i)
        except Exception as e:  # noqa: BLE001
            errors[i] = e

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out, errors


def test_concurrent_resend_of_one_request_acts_once(engine, svc, actor):
    rid = uuid.uuid4()
    out, errors = _race(lambda i: svc.control(KEY, "pause", rid, actor))
    assert errors == [None, None]
    assert sorted(r.replayed for r in out) == [False, True]
    assert all(r.outcome == "applied" and r.control.paused is True for r in out)
    assert len(_events(engine, actor)) == 1


def test_concurrent_reuse_across_jobs_lets_exactly_one_through(engine, svc, actor):
    rid = uuid.uuid4()
    keys = [KEY, OTHER]
    out, errors = _race(lambda i: svc.control(keys[i], "pause", rid, actor))
    assert sum(isinstance(e, ControlConflict) for e in errors) == 1
    assert sum(r is not None for r in out) == 1
    winner = next(r.job_key for r in out if r is not None)
    loser = next(k for k in keys if k != winner)
    assert _flag(engine, winner).paused is True
    loser_row = _flag(engine, loser)
    assert loser_row is None or loser_row.paused is False
    assert len(_events(engine, actor)) == 1


def test_concurrent_pause_and_resume_serialize_on_the_row(engine, svc, actor):
    actions = ["pause", "resume"]
    out, errors = _race(lambda i: svc.control(KEY, actions[i], uuid.uuid4(), actor))
    assert errors == [None, None]
    final = _flag(engine).paused
    # Whichever committed last decided the flag; the audit trail agrees with it.
    events = _events(engine, actor)
    assert len(events) == 2
    applied = [e for e in events if e.outcome == "applied"]
    assert applied and (applied[-1].action == "pause") == final


def test_executor_stamp_confirms_only_the_current_pause(engine, svc, actor):
    svc.control(KEY, "pause", uuid.uuid4(), actor)
    with engine.begin() as c:  # what the worker does when it skips
        c.execute(text("UPDATE automation_controls SET honored_at = now() WHERE job_key = :k AND paused"), {"k": KEY})
    with sessionmaker(bind=engine)() as db:
        read = read_db_state(db)
    row = read.controls[KEY]
    assert row.paused and row.honored_at >= row.changed_at

    svc.control(KEY, "resume", uuid.uuid4(), actor)
    again = svc.control(KEY, "pause", uuid.uuid4(), actor)
    # The stamp from the previous pause predates this change.
    assert again.control.confirmed is False
