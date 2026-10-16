"""FEAT-automation-admin 3c — owner run-now.

No database here: the claim transaction, cooldown, replay and the two-connection
race are tests/integration/test_automation_runs_db.py. This file pins:

* which jobs carry the run control — exactly the shared RUNNABLE_JOB_KEYS;
* how a run row reads (cooldown, stalled, unknown when the read failed);
* the invoker: async Event on blogWorkerLambda only, the shared payload, no retry,
  explicit timeouts, never raising, and OFF under ENV=local|dev;
* the route: owner gate before any body handling, 404 / 409 (typed) / 503 mapping.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from myblog_shared_db.automation import RUN_ID_FIELD, RUNNABLE_JOB_KEYS

from app.api.schemas import AutomationRunResult, AutomationRunState, AutomationRunView
from app.services import automation_catalog as catalog
from app.services import automation_invoke
from app.services.automation_service import (
    AutomationService,
    ControlConflict,
    DbRead,
    RunRejected,
    RunRow,
    assemble,
    run_state,
)

NOW = datetime(2026, 10, 16, 10, 0, 0, tzinfo=timezone.utc)
JOBS = {j.key: j for j in catalog.JOBS}
COOLDOWN = timedelta(seconds=catalog.RUN_COOLDOWN_SECONDS)


# ── catalog ─────────────────────────────────────────────────────────────────────


class TestCatalog:
    def test_run_is_on_exactly_the_shared_runnable_keys(self):
        assert {j.key for j in catalog.JOBS if "run" in j.controls} == RUNNABLE_JOB_KEYS

    @pytest.mark.parametrize("key", ["backend_warm_ping", "music_warm_ping", "gpt-translate-poller", "sqs_album_sync"])
    def test_warm_pings_agents_and_messages_cannot_run(self, key):
        assert "run" not in JOBS[key].controls

    def test_every_runnable_job_runs_on_the_invokable_function(self):
        assert {JOBS[k].executor for k in RUNNABLE_JOB_KEYS} == {catalog.RUN_FUNCTION_NAME} == {"blogWorkerLambda"}

    def test_cooldown_is_the_owner_s_flat_ten_minutes(self):
        assert catalog.RUN_COOLDOWN_SECONDS == 600


# ── state reading ───────────────────────────────────────────────────────────────


def _run(outcome="invoked", requested=NOW - timedelta(minutes=3), started=None, finished=None,
         run_outcome=None, cooldown_from="requested"):
    return RunRow(
        request_id=uuid.uuid4(), outcome=outcome, requested_at=requested,
        invoked_at=requested if outcome == "invoked" else None, started_at=started,
        finished_at=finished, run_outcome=run_outcome,
        cooldown_from=requested if cooldown_from == "requested" else cooldown_from,
    )


class TestRunState:
    def test_never_run_can_run_now(self):
        assert run_state(None, NOW) == AutomationRunState(status="ok")

    def test_failed_read_is_unknown_never_can_run(self):
        s = run_state(None, NOW, read_ok=False)
        assert s.status == "unknown" and s.last is None and s.cooldown_until is None

    def test_cooldown_runs_ten_minutes_from_the_counting_row(self):
        s = run_state(_run(requested=NOW - timedelta(minutes=3)), NOW)
        assert s.cooldown_until == NOW - timedelta(minutes=3) + COOLDOWN
        assert run_state(_run(requested=NOW - timedelta(minutes=11)), NOW).cooldown_until is None

    def test_a_failed_invoke_shows_but_starts_no_cooldown(self):
        s = run_state(_run(outcome="invoke_failed", cooldown_from=None), NOW)
        assert s.last.outcome == "invoke_failed" and s.cooldown_until is None

    def test_stalled_only_after_one_cooldown_without_an_end(self):
        started = NOW - COOLDOWN - timedelta(seconds=1)
        assert run_state(_run(requested=started, started=started), NOW).stalled is True
        recent = NOW - timedelta(minutes=4)
        assert run_state(_run(started=recent), NOW).stalled is False
        assert run_state(_run(requested=started, started=started, finished=NOW, run_outcome="ok"), NOW).stalled is False
        assert run_state(_run(requested=started), NOW).stalled is False  # never started: not "stalled"


class TestSnapshot:
    def test_run_block_only_on_runnable_jobs(self):
        read = DbRead(observed_at=NOW, queues={}, heartbeats=[], runs={"album_ingest": _run()})
        by_key = {j.key: j for j in assemble(read, fallback_now=NOW).jobs}
        assert by_key["album_ingest"].run.last.outcome == "invoked"
        assert by_key["genius_fetch"].run == AutomationRunState(status="ok")
        assert by_key["gpt-translate-poller"].run is None and by_key["backend_warm_ping"].run is None
        assert sum(j.run is not None for j in by_key.values()) == len(RUNNABLE_JOB_KEYS)

    def test_failed_db_read_makes_every_run_unknown(self):
        runs = [j.run for j in assemble(None, fallback_now=NOW).jobs if j.run is not None]
        assert runs and all(r.status == "unknown" for r in runs)


# ── service orchestration (DB stubbed) ──────────────────────────────────────────


class TestServiceRun:
    def test_unknown_or_non_runnable_key_never_touches_the_db_or_lambda(self):
        factory, invoker = MagicMock(), MagicMock()
        svc = AutomationService(factory, invoker=invoker)
        assert svc.run("gpt-translate-poller", uuid.uuid4(), "owner") is None
        factory.assert_not_called()
        invoker.assert_not_called()

    def test_invoke_happens_between_two_closed_sessions(self):
        events = []
        rid = uuid.uuid4()

        class Session:
            def __enter__(self):
                events.append("open")
                return self

            def __exit__(self, *a):
                events.append("close")

        row = _run()

        def invoker(key, request_id):
            events.append(("invoke", key, request_id))
            return True

        with patch("app.services.automation_service._claim_run", return_value=None), \
                patch("app.services.automation_service._record_invoke", return_value=(row, NOW)) as record:
            result = AutomationService(Session, invoker=invoker).run("album_ingest", rid, "owner")
        assert events == ["open", "close", ("invoke", "album_ingest", rid), "open", "close"]
        assert record.call_args.args[1:] == (rid, "invoked")
        assert result.outcome == "invoked" and result.replayed is False

    @pytest.mark.parametrize("invoker", [None, lambda key, rid: False])
    def test_no_invoker_or_a_refusal_records_invoke_failed(self, invoker):
        row = _run(outcome="invoke_failed", cooldown_from=None)
        with patch("app.services.automation_service._claim_run", return_value=None), \
                patch("app.services.automation_service._record_invoke", return_value=(row, NOW)) as record:
            AutomationService(MagicMock(), invoker=invoker).run("album_ingest", uuid.uuid4(), "owner")
        assert record.call_args.args[2] == "invoke_failed"

    def test_a_replay_never_invokes(self):
        invoker = MagicMock()
        replay = AutomationRunResult(request_id=uuid.uuid4(), job_key="album_ingest", outcome="invoked",
                                     replayed=True, run=AutomationRunState(status="ok"))
        with patch("app.services.automation_service._claim_run", return_value=replay), \
                patch("app.services.automation_service._record_invoke") as record:
            assert AutomationService(MagicMock(), invoker=invoker).run("album_ingest", replay.request_id, "o") is replay
        invoker.assert_not_called()
        record.assert_not_called()

    def test_a_rejection_never_invokes(self):
        invoker = MagicMock()
        with patch("app.services.automation_service._claim_run", side_effect=RunRejected("job_paused")):
            with pytest.raises(RunRejected):
                AutomationService(MagicMock(), invoker=invoker).run("album_ingest", uuid.uuid4(), "o")
        invoker.assert_not_called()


# ── invoker ─────────────────────────────────────────────────────────────────────


class TestInvoker:
    def test_async_event_on_the_worker_with_the_shared_payload(self):
        client = MagicMock()
        client.invoke.return_value = {"StatusCode": 202}
        rid = uuid.uuid4()
        assert automation_invoke.invoke_with(client)("release_upcoming_poll_itunes", rid) is True
        kwargs = client.invoke.call_args.kwargs
        assert kwargs["FunctionName"] == "blogWorkerLambda" and kwargs["InvocationType"] == "Event"
        assert json.loads(kwargs["Payload"]) == {
            "job": "release_upcoming_poll", "mode": "itunes", RUN_ID_FIELD: str(rid),
        }

    def test_a_non_202_answer_is_a_failure(self):
        client = MagicMock()
        client.invoke.return_value = {"StatusCode": 200}
        assert automation_invoke.invoke_with(client)("album_ingest", uuid.uuid4()) is False

    def test_an_aws_error_is_a_failure_logged_by_code_only(self, caplog):
        client = MagicMock()
        err = Exception("arn:aws:lambda:ap-northeast-2:123456789012:function:blogWorkerLambda denied")
        err.response = {"Error": {"Code": "AccessDeniedException"}}
        client.invoke.side_effect = err
        with caplog.at_level(logging.ERROR):
            assert automation_invoke.invoke_with(client)("album_ingest", uuid.uuid4()) is False
        assert "AccessDeniedException" in caplog.text and "123456789012" not in caplog.text

    @pytest.mark.parametrize("env", ["local", "dev"])
    def test_off_under_local_and_dev(self, env):
        with patch("boto3.client") as boto:
            assert automation_invoke.build_invoker(env, "ap-northeast-2")("album_ingest", uuid.uuid4()) is False
        boto.assert_not_called()

    def test_prod_client_has_explicit_timeouts_and_no_retry(self):
        client = MagicMock()
        client.invoke.return_value = {"StatusCode": 202}
        with patch("boto3.client", return_value=client) as boto:
            assert automation_invoke.build_invoker("prod", "ap-northeast-2")("album_ingest", uuid.uuid4()) is True
        assert boto.call_args.args == ("lambda",)
        config = boto.call_args.kwargs["config"]
        assert config.connect_timeout == automation_invoke.CONNECT_TIMEOUT_SECONDS
        assert config.read_timeout == automation_invoke.READ_TIMEOUT_SECONDS
        assert config.retries == {"max_attempts": 1, "mode": "standard"}


# ── route ───────────────────────────────────────────────────────────────────────

PATH = "/api/admin/automation/runs"


def _prod_settings(owner_sub: str):
    return SimpleNamespace(
        ENV="prod",
        COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
        COGNITO_REGION="ap-northeast-2",
        COGNITO_ALLOWED_CLIENT_IDS="client-1",
        OWNER_SUB=owner_sub,
    )


def _body(**over):
    body = {"job_key": "album_ingest", "request_id": str(uuid.uuid4())}
    body.update(over)
    return body


def _result(key, rid, outcome="invoked", replayed=False):
    return AutomationRunResult(
        request_id=rid, job_key=key, outcome=outcome, replayed=replayed,
        run=AutomationRunState(status="ok", last=AutomationRunView(request_id=rid, outcome=outcome, requested_at=NOW),
                               cooldown_until=NOW + COOLDOWN),
    )


@pytest.fixture
def svc(app):
    from app.di import get_automation_service

    s = MagicMock(spec=AutomationService)
    s.run.side_effect = lambda key, rid, actor: (
        None if "run" not in getattr(JOBS.get(key), "controls", ()) else _result(key, rid)
    )
    app.dependency_overrides[get_automation_service] = lambda: s
    return s


def _as(app, sub):
    from app.core.auth import require_cognito_token

    app.dependency_overrides[require_cognito_token] = lambda: {"sub": sub}


def _prod():
    import app.core.auth as auth_module

    return patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub"))


class TestRoute:
    def test_local_runs_and_records_the_single_owner_sentinel(self, client, svc):
        body = _body()
        resp = client.post(PATH, json=body)
        assert resp.status_code == 200 and resp.json()["outcome"] == "invoked"
        key, rid, actor = svc.run.call_args.args
        assert (key, str(rid), actor) == ("album_ingest", body["request_id"], "owner")

    def test_owner_in_prod_is_recorded_by_verified_sub(self, client, app, svc):
        _as(app, "owner-sub")
        with _prod():
            assert client.post(PATH, json=_body()).status_code == 200
        assert svc.run.call_args.args[2] == "owner-sub"

    def test_a_failed_invoke_is_a_200_with_its_outcome(self, client, svc):
        svc.run.side_effect = lambda key, rid, actor: _result(key, rid, outcome="invoke_failed")
        resp = client.post(PATH, json=_body())
        assert resp.status_code == 200 and resp.json()["outcome"] == "invoke_failed"

    @pytest.mark.parametrize("key", ["backend_warm_ping", "gpt-translate-poller", "tracks", "ALBUM_INGEST"])
    def test_key_without_a_run_control_is_404(self, client, svc, key):
        assert client.post(PATH, json=_body(job_key=key)).status_code == 404

    def test_paused_job_is_a_typed_409(self, client, svc):
        svc.run.side_effect = RunRejected("job_paused")
        resp = client.post(PATH, json=_body())
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "job_paused"
        assert "retry-after" not in resp.headers

    def test_cooldown_is_a_typed_409_with_retry_after(self, client, svc):
        until = NOW + timedelta(seconds=321)
        svc.run.side_effect = RunRejected("cooldown", until, 321)
        resp = client.post(PATH, json=_body())
        assert resp.status_code == 409 and resp.headers["retry-after"] == "321"
        detail = resp.json()["detail"]
        assert detail["code"] == "cooldown" and detail["retry_after_seconds"] == 321
        assert datetime.fromisoformat(detail["cooldown_until"]) == until

    def test_reused_request_id_is_a_typed_409(self, client, svc):
        svc.run.side_effect = ControlConflict()
        resp = client.post(PATH, json=_body())
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "request_id_conflict"

    def test_db_failure_is_503_and_asks_for_a_resend(self, client, svc):
        svc.run.side_effect = OperationalError("stmt", {}, Exception("down"))
        resp = client.post(PATH, json=_body())
        assert resp.status_code == 503 and "same request_id" in resp.json()["detail"]

    @pytest.mark.parametrize("over", [
        {"request_id": "not-a-uuid"},
        {"job_key": ""},
        {"job_key": "x" * 65},
        {"action": "run"},       # no action field on this route
        {"payload": {"job": "disc_no_backfill"}},  # the payload is never the caller's
    ])
    def test_malformed_body_is_422_for_the_owner(self, client, svc, over):
        assert client.post(PATH, json=_body(**over)).status_code == 422
        svc.run.assert_not_called()

    def test_missing_token_is_401_in_prod(self, client, svc):
        with _prod():
            assert client.post(PATH, json=_body()).status_code == 401
        svc.run.assert_not_called()

    @pytest.mark.parametrize("over", [{}, {"job_key": "tracks"}, {"request_id": "x"}])
    def test_non_owner_is_403_before_any_body_handling(self, client, app, svc, over):
        _as(app, "some-member")
        with _prod():
            assert client.post(PATH, json=_body(**over)).status_code == 403
        svc.run.assert_not_called()
