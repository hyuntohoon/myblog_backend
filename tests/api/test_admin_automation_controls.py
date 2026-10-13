"""FEAT-automation-admin 3b — owner pause/resume controls.

No database here: the transaction order, idempotency and the two-connection race
are tests/integration/test_automation_controls_db.py. This file pins:

* which jobs carry the pause control — exactly the shared PAUSABLE_JOB_KEYS;
* how a flag row reads (confirmed only when honored_at >= changed_at; unknown when
  the read failed; "overdue" only after one schedule interval);
* the route: owner gate before any body handling, 404 / 409 / 503 mapping, and the
  actor recorded from the verified sub (single-owner sentinel under ENV=local).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from myblog_shared_db.automation import PAUSABLE_JOB_KEYS

from app.api.schemas import AutomationControlResult, AutomationControlState
from app.services import automation_catalog as catalog
from app.services.automation_service import (
    AutomationService,
    ControlConflict,
    ControlRow,
    DbRead,
    assemble,
    control_state,
)

NOW = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
JOBS = {j.key: j for j in catalog.JOBS}


# ── catalog ─────────────────────────────────────────────────────────────────────


class TestCatalog:
    def test_pause_is_on_exactly_the_shared_pausable_keys(self):
        # Every shared key must exist in the catalog, and nothing else may carry
        # "pause": a key the executors do not map would be a pause that never applies.
        assert {j.key for j in catalog.JOBS if "pause" in j.controls} == PAUSABLE_JOB_KEYS

    @pytest.mark.parametrize("key", ["backend_warm_ping", "music_warm_ping", "buckit-nightly", "sqs_dropped_jobs"])
    def test_keep_warm_pings_agents_and_messages_have_no_control(self, key):
        assert JOBS[key].controls == ()


# ── state reading ───────────────────────────────────────────────────────────────


def _row(paused=True, changed=NOW - timedelta(minutes=5), honored=None):
    return ControlRow(paused=paused, changed_at=changed, honored_at=honored)


class TestControlState:
    def test_never_controlled_is_running_not_unknown(self):
        assert control_state(JOBS["album_ingest"], None, NOW) == AutomationControlState(paused=False)

    def test_failed_read_is_unknown(self):
        s = control_state(JOBS["album_ingest"], None, NOW, read_ok=False)
        assert s.paused is None and s.confirmed is False and s.confirmation_overdue is False

    def test_confirmed_only_when_honoured_after_the_change(self):
        job = JOBS["lyrics_incremental"]
        assert control_state(job, _row(honored=NOW - timedelta(minutes=1)), NOW).confirmed is True
        # A stamp left over from an earlier pause does not confirm this one.
        assert control_state(job, _row(honored=NOW - timedelta(minutes=9)), NOW).confirmed is False

    def test_resumed_is_never_confirmed_or_overdue(self):
        s = control_state(JOBS["album_ingest"], _row(paused=False, changed=NOW - timedelta(days=9)), NOW)
        assert s.paused is False and not s.confirmed and not s.confirmation_overdue

    def test_overdue_waits_one_schedule_interval(self):
        weekly = JOBS["isrc_backfill"]  # 7-day interval
        assert control_state(weekly, _row(changed=NOW - timedelta(days=6)), NOW).confirmation_overdue is False
        assert control_state(weekly, _row(changed=NOW - timedelta(days=8)), NOW).confirmation_overdue is True
        fifteen = JOBS["lyrics_incremental"]
        assert control_state(fifteen, _row(changed=NOW - timedelta(minutes=10)), NOW).confirmation_overdue is False
        assert control_state(fifteen, _row(changed=NOW - timedelta(minutes=20)), NOW).confirmation_overdue is True

    def test_gpt_agent_grace_is_its_staleness_bound(self):
        gpt = JOBS["gpt-translate-poller"]
        assert control_state(gpt, _row(changed=NOW - timedelta(minutes=14)), NOW).confirmation_overdue is False
        assert control_state(gpt, _row(changed=NOW - timedelta(minutes=16)), NOW).confirmation_overdue is True


class TestSnapshot:
    def _read(self, controls):
        return DbRead(observed_at=NOW, queues={}, heartbeats=[], controls=controls)

    def test_control_block_only_on_pausable_jobs(self):
        snap = assemble(self._read({"album_ingest": _row()}), fallback_now=NOW)
        by_key = {j.key: j for j in snap.jobs}
        assert by_key["album_ingest"].control.paused is True
        assert by_key["genius_fetch"].control.paused is False
        assert by_key["backend_warm_ping"].control is None
        assert sum(j.control is not None for j in snap.jobs) == len(PAUSABLE_JOB_KEYS)

    def test_failed_db_read_makes_every_control_unknown(self):
        snap = assemble(None, fallback_now=NOW)
        controls = [j.control for j in snap.jobs if j.control is not None]
        assert controls and all(c.paused is None for c in controls)


# ── route ───────────────────────────────────────────────────────────────────────

PATH = "/api/admin/automation/controls"


def _prod_settings(owner_sub: str):
    return SimpleNamespace(
        ENV="prod",
        COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
        COGNITO_REGION="ap-northeast-2",
        COGNITO_ALLOWED_CLIENT_IDS="client-1",
        OWNER_SUB=owner_sub,
    )


def _body(**over):
    body = {"job_key": "album_ingest", "action": "pause", "request_id": str(uuid.uuid4())}
    body.update(over)
    return body


def _result(body, outcome="applied", replayed=False):
    return AutomationControlResult(
        request_id=body["request_id"], job_key=body["job_key"], action=body["action"],
        outcome=outcome, replayed=replayed, control=AutomationControlState(paused=True, changed_at=NOW),
    )


@pytest.fixture
def svc(app):
    from app.di import get_automation_service

    s = MagicMock(spec=AutomationService)
    s.control.side_effect = lambda key, action, rid, actor: (
        None if "pause" not in getattr(JOBS.get(key), "controls", ()) else
        _result({"job_key": key, "action": action, "request_id": str(rid)})
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
    def test_local_applies_and_records_the_single_owner_sentinel(self, client, svc):
        body = _body()
        resp = client.post(PATH, json=body)
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "applied" and resp.json()["control"]["paused"] is True
        key, action, rid, actor = svc.control.call_args.args
        assert (key, action, str(rid), actor) == ("album_ingest", "pause", body["request_id"], "owner")

    def test_owner_in_prod_is_recorded_by_verified_sub(self, client, app, svc):
        _as(app, "owner-sub")
        with _prod():
            assert client.post(PATH, json=_body(action="resume")).status_code == 200
        assert svc.control.call_args.args[3] == "owner-sub"

    @pytest.mark.parametrize("key", ["backend_warm_ping", "buckit-nightly", "tracks", "ALBUM_INGEST"])
    def test_key_without_a_pause_control_is_404(self, client, svc, key):
        assert client.post(PATH, json=_body(job_key=key)).status_code == 404

    def test_reused_request_id_for_another_action_is_409(self, client, svc):
        svc.control.side_effect = ControlConflict()
        assert client.post(PATH, json=_body()).status_code == 409

    def test_db_failure_is_503_never_a_claimed_change(self, client, svc):
        svc.control.side_effect = OperationalError("stmt", {}, Exception("down"))
        resp = client.post(PATH, json=_body())
        assert resp.status_code == 503
        assert "nothing was changed" in resp.json()["detail"]

    @pytest.mark.parametrize("over", [
        {"action": "run"},
        {"action": "cancel"},
        {"request_id": "not-a-uuid"},
        {"job_key": ""},
        {"job_key": "x" * 65},
        {"extra": "field"},
    ])
    def test_malformed_body_is_422_for_the_owner(self, client, svc, over):
        assert client.post(PATH, json=_body(**over)).status_code == 422
        svc.control.assert_not_called()

    def test_missing_token_is_401_in_prod(self, client, svc):
        with _prod():
            assert client.post(PATH, json=_body()).status_code == 401
        svc.control.assert_not_called()

    @pytest.mark.parametrize("over", [{}, {"job_key": "tracks"}, {"action": "run"}, {"request_id": "x"}])
    def test_non_owner_is_403_before_any_body_handling(self, client, app, svc, over):
        # A member cannot probe which keys exist (404) or what the body takes (422).
        _as(app, "some-member")
        with _prod():
            assert client.post(PATH, json=_body(**over)).status_code == 403
        svc.control.assert_not_called()
