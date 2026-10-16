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
        {"action": "no_lyrics"}, {"action": "unlock"},
        {"action": "pick", "lrclib_id": 34738767}, {"action": "reject", "lrclib_id": 8996695},
    ])
    def test_each_action_reaches_the_service(self, client, svc, body):
        assert client.post(f"{BASE}/decision", json=body).status_code == 200
        kw = svc.decide.call_args.kwargs
        assert (kw["action"], kw["lrclib_id"]) == (body["action"], body.get("lrclib_id"))
        assert kw["actor"] == "owner"  # ENV=local single-owner sentinel


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
            assert c.send_lyrics_pick("tid", "42", "owner-sub") is True
        assert [json.loads(b) for b in sent] == [
            {"job": "lyrics_reassessment", "track_id": "tid", "cooldown_sec": 0},
            {"job": "lyrics_pick", "track_id": "tid", "lrclib_id": 42, "actor": "owner-sub"},
        ]

    def test_no_queue_sends_nothing(self):
        from app.clients.sqs_client import SqsClient

        with patch("boto3.client") as boto:
            assert SqsClient(queue_url="").send_lyrics_pick("tid", 1, "s") is False
        boto.assert_not_called()
