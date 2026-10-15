"""FEAT-spotify-library-autosync 3b — the sync POST gate against real Postgres.

The board now requests a sync on every entry, so what keeps the queue from filling is
this gate: a row lock on spotify_library_sync_runs, a commit before the SQS send, and
a restore when the send does not happen. None of that is visible to the mocked route
tests. Rows are committed (the service commits its own transactions) and removed
with the throwaway user at the end; the run row goes with its bucket (ON DELETE
CASCADE).

V61 must already be applied — asserted, never created here.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.bucket_service import BucketService

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True, pool_size=4)
    with eng.connect() as c:
        assert c.execute(text("SELECT to_regclass('spotify_library_sync_runs')")).scalar() is not None, "V61 missing"
    yield eng
    eng.dispose()


@pytest.fixture
def bucket(engine):
    with engine.begin() as c:
        user_id = c.execute(
            text("INSERT INTO users (id, handle, display_name) VALUES (gen_random_uuid(), :h, :h) RETURNING id"),
            {"h": f"it-slib-{uuid.uuid4().hex[:10]}"},
        ).scalar()
        bucket_id = c.execute(
            text("INSERT INTO review_buckets (name, position, kind, user_id) "
                 "VALUES ('Spotify', 0, 'spotify_library', :u) RETURNING id"),
            {"u": user_id},
        ).scalar()
    yield bucket_id
    with engine.begin() as c:
        c.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def _run(engine, bucket_id):
    with engine.connect() as c:
        return c.execute(text("SELECT requested_at, finished_at FROM spotify_library_sync_runs WHERE bucket_id = :b"),
                         {"b": bucket_id}).one_or_none()


def _set(engine, bucket_id, **cols):
    sets = ", ".join(f"{k} = {v}" for k, v in cols.items())
    with engine.begin() as c:
        c.execute(text(f"UPDATE spotify_library_sync_runs SET {sets} WHERE bucket_id = :b"), {"b": bucket_id})


class _Send:
    def __init__(self, result=True, exc=None):
        self.calls = 0
        self.result, self.exc = result, exc

    def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def _request(engine, bucket_id, send):
    Session = sessionmaker(bind=engine)
    with Session() as db:
        return BucketService().request_library_sync(db, bucket_id, send)


def test_first_post_queues_and_the_next_joins_it(engine, bucket):
    send = _Send()
    assert _request(engine, bucket, send) == "queued"
    assert _run(engine, bucket).requested_at is not None
    assert _request(engine, bucket, send) == "in_flight"
    assert send.calls == 1


@pytest.mark.parametrize("round_", range(5))
def test_two_tabs_at_once_enqueue_exactly_one_job(engine, bucket, round_):
    # Steady state: the run row already exists and its last pass is long finished.
    # (On a missing row the INSERT … ON CONFLICT serialises the two tabs by itself,
    # which would hide a missing FOR UPDATE.)
    _request(engine, bucket, _Send())
    _set(engine, bucket, requested_at="now() - interval '3 minutes'", finished_at="now() - interval '2 minutes'")
    send = _Send()
    barrier = threading.Barrier(2)
    results = []

    def tab():
        barrier.wait()
        results.append(_request(engine, bucket, send))

    threads = [threading.Thread(target=tab) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["in_flight", "queued"]
    assert send.calls == 1


def test_a_failed_send_restores_the_request(engine, bucket):
    with pytest.raises(RuntimeError):
        _request(engine, bucket, _Send(exc=RuntimeError("sqs down")))
    assert _run(engine, bucket).requested_at is None
    # and the next entry is free to try again
    assert _request(engine, bucket, _Send()) == "queued"


def test_a_no_op_send_restores_the_request(engine, bucket):
    assert _request(engine, bucket, _Send(result=False)) == "queued"
    assert _run(engine, bucket).requested_at is None


def test_a_stale_request_is_requeued(engine, bucket):
    _request(engine, bucket, _Send())
    _set(engine, bucket, requested_at="now() - interval '10 minutes'")
    send = _Send()
    assert _request(engine, bucket, send) == "queued"
    assert send.calls == 1


def test_just_finished_is_debounced_and_an_old_finish_is_not(engine, bucket):
    _request(engine, bucket, _Send())
    _set(engine, bucket, requested_at="now() - interval '20 seconds'", finished_at="now() - interval '5 seconds'")
    send = _Send()
    assert _request(engine, bucket, send) == "debounced"
    assert send.calls == 0
    _set(engine, bucket, requested_at="now() - interval '3 minutes'", finished_at="now() - interval '2 minutes'")
    assert _request(engine, bucket, send) == "queued"
    assert send.calls == 1


def test_state_read_reports_the_run(engine, bucket):
    Session = sessionmaker(bind=engine)
    with Session() as db:
        assert BucketService().get_library_sync_run(db, None) is None
        assert BucketService().get_library_sync_run(db, bucket) is None  # no row yet
    _request(engine, bucket, _Send())
    with Session() as db:
        requested, finished, now = BucketService().get_library_sync_run(db, bucket)
        assert requested is not None and finished is None and now >= requested
