"""FEAT-lyrics-match-correction Step 3a — the owner's lyrics candidate panel, route level.

No database here: what each decision writes is tests/integration/test_lyrics_admin_db.py.
This file pins the owner gate (before any body handling), the status mapping, the body
contract, and that the actor is the verified sub.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from app.api.schemas import (
    AdminLyricsCandidates,
    AdminLyricsCurrent,
    AdminLyricsDecisionResult,
)
from app.services.lyrics_admin_service import (
    LyricsAdminConflict,
    LyricsAdminQueueUnavailable,
    LyricsAdminService,
    LyricsAdminTrackNotFound,
)

SID = "4k6Uh1HXdhtusDW5y8Gbvy"
BASE = f"/api/admin/lyrics/{SID}"
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def svc(app):
    from app.db.session import get_db
    from app.di import get_lyrics_admin_service

    s = MagicMock(spec=LyricsAdminService)
    s.candidates.return_value = AdminLyricsCandidates(
        spotify_track_id=SID, track_title="Bad Habit", current=AdminLyricsCurrent(), stale=True,
    )
    s.refresh.return_value = True
    s.decide.side_effect = lambda db, **kw: AdminLyricsDecisionResult(
        action=kw["action"], outcome="applied", current=AdminLyricsCurrent(),
    )
    app.dependency_overrides[get_lyrics_admin_service] = lambda: s
    app.dependency_overrides[get_db] = lambda: MagicMock()
    yield s
    app.dependency_overrides.clear()


def _prod():
    import app.core.auth as auth_module

    return patch.object(auth_module, "settings", SimpleNamespace(
        ENV="prod", COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
        COGNITO_REGION="ap-northeast-2", COGNITO_ALLOWED_CLIENT_IDS="client-1",
        OWNER_SUB="owner-sub",
    ))


def _as(app, sub):
    from app.core.auth import require_cognito_token

    app.dependency_overrides[require_cognito_token] = lambda: {"sub": sub}


class TestOwnerGate:
    @pytest.mark.parametrize("method,path,body", [
        ("get", f"{BASE}/candidates", None),
        ("post", f"{BASE}/candidates/refresh", None),
        ("post", f"{BASE}/decision", {"action": "pick", "lrclib_id": 1}),
        ("post", f"{BASE}/decision", {"action": "nonsense"}),  # 403 before 422
        ("post", f"{BASE}/decision", {"action": "keep"}),
    ])
    def test_member_is_403_and_nothing_runs(self, client, app, svc, method, path, body):
        _as(app, "some-member")
        with _prod():
            resp = getattr(client, method)(path, **({"json": body} if body is not None else {}))
        assert resp.status_code == 403
        assert not svc.method_calls

    def test_missing_token_is_401(self, client, svc):
        with _prod():
            assert client.get(f"{BASE}/candidates").status_code == 401
        assert not svc.method_calls

    def test_owner_decision_records_the_verified_sub(self, client, app, svc):
        _as(app, "owner-sub")
        with _prod():
            assert client.post(f"{BASE}/decision", json={"action": "no_lyrics"}).status_code == 200
        assert svc.decide.call_args.kwargs["actor"] == "owner-sub"
        assert svc.decide.call_args.kwargs["spotify_track_id"] == SID


class TestRoutes:
    def test_candidates_read(self, client, svc):
        resp = client.get(f"{BASE}/candidates")
        assert resp.status_code == 200
        assert resp.json()["stale"] is True and resp.json()["candidates"] == []

    def test_refresh_is_202(self, client, svc):
        resp = client.post(f"{BASE}/candidates/refresh")
        assert (resp.status_code, resp.json()) == (202, {"enqueued": True})

    @pytest.mark.parametrize("path,method", [
        ("candidates", "get"), ("candidates/refresh", "post"),
    ])
    def test_unknown_track_is_404(self, client, svc, path, method):
        svc.candidates.side_effect = svc.refresh.side_effect = LyricsAdminTrackNotFound(SID)
        assert getattr(client, method)(f"{BASE}/{path}").status_code == 404

    @pytest.mark.parametrize("exc,status", [
        (LyricsAdminTrackNotFound(SID), 404),
        (LyricsAdminConflict("not_a_candidate"), 409),
        (LyricsAdminConflict("rejected"), 409),
        (LyricsAdminQueueUnavailable(), 503),
        (OperationalError("stmt", {}, Exception("down")), 503),
    ])
    def test_decision_error_mapping(self, client, svc, exc, status):
        svc.decide.side_effect = exc
        resp = client.post(f"{BASE}/decision", json={"action": "pick", "lrclib_id": 5})
        assert resp.status_code == status
        if status == 409:
            assert resp.json()["detail"] == exc.code

    @pytest.mark.parametrize("body", [
        {"action": "pick"},
        {"action": "reject"},
        {"action": "pick", "lrclib_id": 0},
        {"action": "pick", "lrclib_id": -3},
        {"action": "delete"},
        {"action": "no_lyrics", "extra": 1},
        {},
    ])
    def test_malformed_body_is_422(self, client, svc, body):
        assert client.post(f"{BASE}/decision", json=body).status_code == 422
        svc.decide.assert_not_called()

    @pytest.mark.parametrize("body", [
        {"action": "no_lyrics"}, {"action": "unlock"}, {"action": "keep"},
        {"action": "pick", "lrclib_id": 34738767}, {"action": "reject", "lrclib_id": 8996695},
    ])
    def test_each_action_reaches_the_service(self, client, svc, body):
        assert client.post(f"{BASE}/decision", json=body).status_code == 200
        kw = svc.decide.call_args.kwargs
        assert (kw["action"], kw["lrclib_id"]) == (body["action"], body.get("lrclib_id"))
        assert kw["actor"] == "owner"  # ENV=local single-owner sentinel


class TestQueueErrors:
    def test_refresh_send_failure_is_503(self, client, svc):
        svc.refresh.side_effect = LyricsAdminQueueUnavailable()
        assert client.post(f"{BASE}/candidates/refresh").status_code == 503

    @pytest.mark.parametrize("method", ["refresh", "pick"])
    def test_a_boto_error_becomes_queue_unavailable(self, method):
        from unittest.mock import MagicMock as MM
        from botocore.exceptions import EndpointConnectionError

        sqs = MM()
        sqs.send_lyrics_recheck.side_effect = sqs.send_lyrics_pick.side_effect = \
            EndpointConnectionError(endpoint_url="https://sqs")
        svc = LyricsAdminService(sqs)
        db = MM()
        track = MM(id="tid")
        db.query.return_value.filter.return_value.one_or_none.return_value = track
        db.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value = None
        db.query.return_value.filter.return_value.first.side_effect = [(5,), None]  # in set, not rejected
        with pytest.raises(LyricsAdminQueueUnavailable):
            if method == "refresh":
                svc.refresh(db, spotify_track_id=SID)
            else:
                svc.decide(db, spotify_track_id=SID, action="pick", lrclib_id=5, actor="o")


class TestSqsMessages:
    def _client(self):
        from app.clients.sqs_client import SqsClient

        sent = []
        c = SqsClient(queue_url="https://sqs.example/q")
        fake = MagicMock()
        fake.send_message.side_effect = lambda **kw: sent.append(kw["MessageBody"])
        return c, fake, sent

    def test_recheck_and_pick_bodies(self):
        import json

        c, fake, sent = self._client()
        with patch("boto3.client", return_value=fake):
            assert c.send_lyrics_recheck("tid") is True
            assert c.send_lyrics_recheck("tid", cooldown_sec=3600) is True
            assert c.send_lyrics_pick("tid", "42", "owner-sub") is True
        assert [json.loads(b) for b in sent] == [
            {"job": "lyrics_reassessment", "track_id": "tid", "cooldown_sec": 0},
            {"job": "lyrics_reassessment", "track_id": "tid", "cooldown_sec": 3600},
            {"job": "lyrics_pick", "track_id": "tid", "lrclib_id": 42, "actor": "owner-sub"},
        ]

    def test_every_sqs_client_has_explicit_timeouts(self):
        from app.clients import sqs_client

        with patch("boto3.client") as boto:
            sqs_client._sqs()
        cfg = boto.call_args.kwargs["config"]
        assert (cfg.connect_timeout, cfg.read_timeout) == (3, 5)
        import inspect
        src = inspect.getsource(sqs_client)
        assert src.count('boto3.client(') == 2  # _sqs and the SSM read, both with config=

    def test_no_queue_sends_nothing(self):
        from app.clients.sqs_client import SqsClient

        with patch("boto3.client") as boto:
            assert SqsClient(queue_url="").send_lyrics_pick("tid", 1, "s") is False
        boto.assert_not_called()


class TestReportQueue:
    @pytest.fixture
    def rsvc(self, app):
        from app.api.schemas import AdminLyricsReports
        from app.db.session import get_db
        from app.di import get_lyrics_report_service
        from app.services.lyrics_report_service import LyricsReportService

        s = MagicMock(spec=LyricsReportService)
        s.queue.return_value = AdminLyricsReports(items=[], total_tracks=0, truncated=False)
        app.dependency_overrides[get_lyrics_report_service] = lambda: s
        app.dependency_overrides[get_db] = lambda: MagicMock()
        yield s
        app.dependency_overrides.clear()

    def test_owner_reads_the_queue(self, client, rsvc):
        resp = client.get("/api/admin/lyrics/reports")
        assert (resp.status_code, resp.json()) == (200, {"items": [], "total_tracks": 0, "truncated": False})

    def test_member_is_403_and_nothing_runs(self, client, app, rsvc):
        _as(app, "some-member")
        with _prod():
            assert client.get("/api/admin/lyrics/reports").status_code == 403
        assert not rsvc.method_calls

    def test_missing_token_is_401(self, client, rsvc):
        with _prod():
            assert client.get("/api/admin/lyrics/reports").status_code == 401
        assert not rsvc.method_calls


class TestKeepDispatch:
    """`keep` must reach its own branch: the dispatch's old default was unlock, which would
    drop the owner's lock on a "what it serves is right"."""

    def test_keep_never_reaches_unlock(self):
        svc = LyricsAdminService(sqs=MagicMock())
        db = MagicMock()
        with patch.object(svc, "_track", return_value=SimpleNamespace(id="tid")), \
                patch.object(svc, "_unlock") as unlock, \
                patch.object(svc, "_keep", return_value="kept") as keep:
            assert svc.decide(db, spotify_track_id=SID, action="keep", lrclib_id=None, actor="o") == "kept"
        keep.assert_called_once()
        unlock.assert_not_called()

    def test_unknown_action_is_refused_not_unlocked(self):
        svc = LyricsAdminService(sqs=MagicMock())
        with patch.object(svc, "_track", return_value=SimpleNamespace(id="tid")), \
                patch.object(svc, "_unlock") as unlock:
            with pytest.raises(ValueError):
                svc.decide(MagicMock(), spotify_track_id=SID, action="bogus", lrclib_id=None, actor="o")
        unlock.assert_not_called()
