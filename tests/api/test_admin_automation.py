"""FEAT-automation-admin 2b — owner automation snapshot.

Three layers, none of which needs a database (the real-Postgres SQL check is
tests/integration/test_automation_db.py):

* the catalog pins the Step 1 inventory and the staleness rule;
* `assemble` pins "no evidence ⇒ unknown, never healthy or empty";
* the service pins the session shape (read-only, SELECT-only, closed before
  assembly) and the route pins the owner gate.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from app.services import automation_catalog as catalog
from app.services.automation_service import (
    AutomationService,
    DbRead,
    HeartbeatRow,
    QueueRead,
    _fold,
    assemble,
)

NOW = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
HEARTBEAT_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")  # V59 ck_automation_heartbeat_key


def _job(snapshot, key):
    return next(j for j in snapshot.jobs if j.key == key)


def _queue(snapshot, key):
    return next(q for q in snapshot.queues if q.key == key)


def _db(heartbeats=(), queues=None):
    return DbRead(observed_at=NOW, queues=dict(queues or {}), heartbeats=list(heartbeats))


def _hb(key, age_seconds, **kw):
    return HeartbeatRow(
        agent_key=key,
        observed_at=NOW - timedelta(seconds=age_seconds),
        enabled=kw.get("enabled", True),
        last_outcome=kw.get("last_outcome"),
        details=kw.get("details", {}),
    )


# ── catalog ─────────────────────────────────────────────────────────────────────


class TestCatalog:
    def test_job_keys_are_unique(self):
        keys = [j.key for j in catalog.JOBS]
        assert len(keys) == len(set(keys))

    def test_inventory_row_counts_match_rfc_tables(self):
        # RFC Step 1 tables: A 18 rules, F 2 schedules, B 12 message rows,
        # C 7 agents (cdp-reaper excluded as dev tooling), E 1 workflow.
        by_group: dict[str, int] = {}
        for j in catalog.JOBS:
            by_group[j.group] = by_group.get(j.group, 0) + 1
        assert by_group == {
            "aws_schedule": 18,
            "aws_scheduler": 2,
            "aws_queue_message": 12,
            "local_agent": 7,
            "other_scheduler": 1,
        }
        assert all(j.key != "cdp-reaper" for j in catalog.JOBS)

    def test_heartbeat_keys_pass_the_v59_check(self):
        for j in catalog.JOBS:
            if j.group == "local_agent":
                assert j.heartbeat_key and HEARTBEAT_KEY.match(j.heartbeat_key), j.key
                assert not j.heartbeat_key.startswith("com.myblog")
            else:
                assert j.heartbeat_key is None, j.key

    @pytest.mark.parametrize(
        ("key", "bound"),
        [
            ("gpt-translate-poller", 900),     # 2 × 60 s → minimum 15 min
            ("research-poller", 900),
            ("genre-heal-poller", 3600),       # 2 × 30 min
            ("buckit-nightly", 172800),        # daily → 48 h
            ("genre-backfill", 172800),
        ],
    )
    def test_stale_bound_is_twice_the_interval_min_15_minutes(self, key, bound):
        job = next(j for j in catalog.JOBS if j.key == key)
        assert job.stale_after_seconds == bound

    def test_every_queue_key_resolves(self):
        known = {q.key for q in catalog.QUEUES} | {
            catalog.translation_queue_key(v.version) for v in catalog.TRANSLATION_VERSIONS
        }
        for j in catalog.JOBS:
            for k in j.queue_keys:
                assert k in known, (j.key, k)

    def test_gpt_worker_owns_all_three_of_its_inputs(self):
        # gpt_queue_worker.CANDIDATES reads requested lyrics rows and pending Genius
        # annotations as well as its own work rows — none of them is frozen.
        gpt = next(j for j in catalog.JOBS if j.key == "gpt-translate-poller")
        assert {"track_lyrics_translations", "track_genius_annotations", "lyrics_album_tracks"} <= set(gpt.queue_keys)
        frozen = {q.key for q in catalog.QUEUES if q.frozen}
        assert not frozen & set(gpt.queue_keys)
        assert [v.version for v in catalog.TRANSLATION_VERSIONS if v.frozen] == ["claude.sonnet/v2"]

    def test_waiting_states_are_labels_the_sql_can_produce(self):
        # A renamed CASE label or a typo here would leave "waiting" silently 0.
        for q in catalog.QUEUES:
            for state in q.waiting_states:
                assert f"'{state}'" in q.sql, (q.key, state)
        for state in catalog.TRANSLATION_WAITING_STATES:
            assert f"'{state}'" in catalog.TRANSLATION_WORK_SQL

    def test_no_controls_in_step_2(self):
        assert all(j.controls == () for j in catalog.JOBS)


# ── fold ────────────────────────────────────────────────────────────────────────


def test_fold_takes_min_oldest_max_last_and_sums_stale():
    a, b, c = NOW - timedelta(hours=3), NOW - timedelta(hours=1), NOW
    read = _fold([("ready", 5, b, b, None), ("retryable_error", 2, a, c, None), ("running", 3, None, b, 2)])
    assert read.counts == {"ready": 5, "retryable_error": 2, "running": 3}
    assert read.oldest_waiting_at == a
    assert read.last_update_at == c
    assert read.stale_running == 2


def test_fold_keeps_stale_null_when_no_row_measures_it():
    assert _fold([("pending", 1, NOW, NOW, None)]).stale_running is None


# ── assemble ────────────────────────────────────────────────────────────────────


class TestAssembleWithoutDb:
    def test_every_job_is_unknown(self):
        snap = assemble(None, fallback_now=NOW)
        assert {j.liveness.status for j in snap.jobs} == {"unknown"}

    def test_queues_are_unknown_not_empty(self):
        snap = assemble(None, fallback_now=NOW)
        for q in snap.queues:
            assert q.status == "unknown"
            assert q.counts is None  # never {} — that would read as an empty queue

    def test_sources_report_the_failure(self):
        snap = assemble(None, fallback_now=NOW)
        status = {s.key: s.status for s in snap.sources}
        assert status == {"catalog": "ok", "db": "unavailable", "heartbeat": "unavailable", "aws": "not_wired"}
        assert snap.generated_at == NOW


class TestAssembleWithDb:
    def test_heartbeat_source_says_how_many_agents_report(self):
        snap = assemble(_db(), fallback_now=NOW)
        hb = next(src for src in snap.sources if src.key == "heartbeat")
        assert hb.detail == "0 agent(s) reporting."

    def test_no_heartbeat_means_unknown_for_every_agent(self):
        # 2a shipped the table empty; until 2e every agent must render unknown.
        snap = assemble(_db(), fallback_now=NOW)
        agents = [j for j in snap.jobs if j.group == "local_agent"]
        assert agents and all(j.liveness.status == "unknown" for j in agents)
        assert all(j.liveness.status != "fresh" for j in snap.jobs)

    def test_fresh_heartbeat_inside_the_bound(self):
        snap = assemble(_db([_hb("gpt-translate-poller", 60)]), fallback_now=NOW)
        live = _job(snap, "gpt-translate-poller").liveness
        assert live.status == "fresh"
        assert live.age_seconds == 60
        assert live.observed_at == NOW - timedelta(seconds=60)

    @pytest.mark.parametrize(("age", "status"), [(900, "fresh"), (901, "stale")])
    def test_bound_edge(self, age, status):
        snap = assemble(_db([_hb("research-poller", age)]), fallback_now=NOW)
        assert _job(snap, "research-poller").liveness.status == status

    def test_heartbeat_a_few_seconds_ahead_is_fresh(self):
        # Committed after the reader's now() under READ COMMITTED — a real fresh row.
        snap = assemble(_db([_hb("gpt-translate-poller", -3)]), fallback_now=NOW)
        live = _job(snap, "gpt-translate-poller").liveness
        assert (live.status, live.age_seconds) == ("fresh", 0)

    def test_heartbeat_from_the_future_is_unknown(self):
        snap = assemble(_db([_hb("gpt-translate-poller", -30)]), fallback_now=NOW)
        assert _job(snap, "gpt-translate-poller").liveness.status == "unknown"

    def test_aws_and_sqs_rows_stay_unknown(self):
        snap = assemble(_db([_hb("gpt-translate-poller", 10)]), fallback_now=NOW)
        for j in snap.jobs:
            if j.location != "mac":
                assert j.liveness.status == "unknown", j.key

    def test_uncatalogued_heartbeat_is_listed_but_not_attached(self):
        snap = assemble(_db([_hb("mystery-agent", 5)]), fallback_now=NOW)
        assert [(h.agent_key, h.catalogued) for h in snap.heartbeats] == [("mystery-agent", False)]
        assert all(j.liveness.status != "fresh" for j in snap.jobs)

    def test_queue_with_no_rows_is_ok_and_empty(self):
        snap = assemble(_db(), fallback_now=NOW)
        q = _queue(snap, "album_research")
        assert q.status == "ok"
        assert q.counts == {}
        assert q.observed_at == NOW

    def test_queue_counts_flow_through(self):
        read = QueueRead({"queued": 2, "done": 87}, NOW - timedelta(hours=2), NOW, 0)
        snap = assemble(_db(queues={"album_research": read}), fallback_now=NOW)
        q = _queue(snap, "album_research")
        assert q.counts == {"queued": 2, "done": 87}
        assert q.oldest_waiting_at == NOW - timedelta(hours=2)
        assert q.waiting_states == ["queued"]

    def test_known_translation_versions_always_listed(self):
        snap = assemble(_db(), fallback_now=NOW)
        keys = {q.key for q in snap.queues}
        for v in catalog.TRANSLATION_VERSIONS:
            assert catalog.translation_queue_key(v.version) in keys
        assert _queue(snap, catalog.translation_queue_key("claude.sonnet/v2")).frozen is True

    def test_unknown_translation_version_is_surfaced(self):
        key = catalog.translation_queue_key("someone/new-v9")
        snap = assemble(_db(queues={key: QueueRead({"ready": 4}, NOW, NOW, 0)}), fallback_now=NOW)
        q = _queue(snap, key)
        assert q.counts == {"ready": 4}
        assert q.frozen is False


# ── service: session shape ──────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._rows[0][0]


class _FakeSession:
    def __init__(self, fail_on=None):
        self.statements: list[str] = []
        self.closed = False
        self._fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, stmt):
        sql = " ".join(str(stmt).split())
        self.statements.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise OperationalError(sql, {}, Exception("boom"))
        if sql == "SELECT now()":
            return _FakeResult([(NOW,)])
        return _FakeResult([])


class TestServiceSession:
    def test_read_only_select_only_and_closed(self):
        session = _FakeSession()
        snap = AutomationService(lambda: session).snapshot()

        assert session.statements[0] == "SET TRANSACTION READ ONLY"
        assert all(s.startswith("SELECT") for s in session.statements[1:]), session.statements
        assert session.closed
        assert snap.generated_at == NOW
        assert {s.key: s.status for s in snap.sources}["db"] == "ok"

    def test_every_queue_table_is_read(self):
        session = _FakeSession()
        AutomationService(lambda: session).snapshot()
        joined = "\n".join(session.statements)
        for table in [q.table for q in catalog.QUEUES] + ["lyrics_translation_work", "automation_heartbeats"]:
            assert f"FROM {table}" in joined, table

    def test_db_error_degrades_to_unknown(self):
        session = _FakeSession(fail_on="automation_heartbeats")
        snap = AutomationService(lambda: session).snapshot()
        assert session.closed
        assert {s.key: s.status for s in snap.sources}["db"] == "unavailable"
        # A partial read is discarded, not half-shown.
        assert all(q.status == "unknown" for q in snap.queues)


# ── route: owner gate ───────────────────────────────────────────────────────────

PATH = "/api/admin/automation"


def _prod_settings(owner_sub: str):
    return SimpleNamespace(
        ENV="prod",
        COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
        COGNITO_REGION="ap-northeast-2",
        COGNITO_ALLOWED_CLIENT_IDS="client-1",
        OWNER_SUB=owner_sub,
    )


@pytest.fixture
def svc(app):
    from app.di import get_automation_service

    s = MagicMock(spec=AutomationService)
    s.snapshot.return_value = assemble(None, fallback_now=NOW)
    app.dependency_overrides[get_automation_service] = lambda: s
    return s


class TestRoute:
    def test_local_returns_the_snapshot(self, client, svc):
        resp = client.get(PATH)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"generated_at", "catalog_as_of", "sources", "jobs", "queues", "heartbeats"}
        assert len(body["jobs"]) == len(catalog.JOBS)
        svc.snapshot.assert_called_once()

    def test_missing_token_is_401_in_prod(self, client, svc):
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(PATH)
        assert resp.status_code == 401
        svc.snapshot.assert_not_called()

    def test_non_owner_is_403_in_prod(self, client, app, svc):
        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token

        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "some-member"}
        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(PATH)
        assert resp.status_code == 403
        svc.snapshot.assert_not_called()

    def test_unset_owner_sub_is_503_in_prod(self, client, app, svc):
        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token

        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "owner-sub"}
        with patch.object(auth_module, "settings", _prod_settings(owner_sub="")):
            resp = client.get(PATH)
        assert resp.status_code == 503
        svc.snapshot.assert_not_called()

    def test_owner_reaches_the_service_in_prod(self, client, app, svc):
        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token

        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "owner-sub"}
        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(PATH)
        assert resp.status_code == 200
        svc.snapshot.assert_called_once()


def test_ci_schema_ref_matches_the_shared_db_pin():
    """The integration job loads shared_db's canonical schema at a pinned ref.

    Moving requirements.txt without it left CI on a schema missing the table the
    code now reads (caught in review of this change; #178 moved both together).
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    pin = re.search(r"myblog_shared_db\.git@([0-9a-f]{40})", (root / "requirements.txt").read_text())
    refs = re.findall(
        r"repository: hyuntohoon/myblog_shared_db\s+ref: ([0-9a-f]{40})",
        (root / ".github/workflows/deploy.yml").read_text(),
    )
    assert pin and refs and set(refs) == {pin.group(1)}
