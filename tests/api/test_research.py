from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.di import get_research_service
from app.services.research_service import (
    AlbumNotFoundError,
    ResearchService,
    ResearchStateError,
)


# AUDIT-2026-07-26 A-3: the routes now parse the id before it can reach a uuid
# column, so route-level tests must use real UUIDs. The service-level tests below
# call the service directly, bypass the route, and keep their opaque ids.
ALB1 = "11111111-1111-4111-8111-111111111111"
ALB2 = "22222222-2222-4222-8222-222222222222"
ALB3 = "33333333-3333-4333-8333-333333333333"
ALB_NO_NOTE = "44444444-4444-4444-8444-444444444444"
ALB_MISSING = "55555555-5555-4555-8555-555555555555"


def _row(album_id=None, status="done", result_md="기존 노트", refine_count=0,
         last_instruction=None):
    return SimpleNamespace(
        album_id=album_id or ALB1,
        prompt_version="v2",
        status=status,
        model="claude-opus-4-8",
        result_md=result_md,
        tokens_in=1000,
        tokens_out=200,
        search_count=0,
        error=None,
        refine_count=refine_count,
        last_instruction=last_instruction,
        requested_at=None,
        started_at=None,
        finished_at=None,
    )


def _override(app, svc):
    from app.db.session import get_db

    app.dependency_overrides[get_research_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


# ── route dispatch (TestClient + mocked service) ─────────────────────────────────

class TestGetResearch:
    def test_existing_note_returns_200(self, client, app):
        svc = MagicMock()
        svc.get_research.return_value = _row()
        _override(app, svc)

        resp = client.get(f"/api/research/albums/{ALB1}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["album_id"] == ALB1
        assert body["status"] == "done"
        assert body["result_md"] == "기존 노트"
        app.dependency_overrides.clear()

    def test_missing_note_returns_404(self, client, app):
        svc = MagicMock()
        svc.get_research.return_value = None
        _override(app, svc)

        resp = client.get(f"/api/research/albums/{ALB_NO_NOTE}")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestResearchStatusMap:
    def test_batched_status_returns_map(self, client, app):
        svc = MagicMock()
        svc.status_map.return_value = {ALB1: "done", ALB2: "queued"}
        _override(app, svc)

        resp = client.get(f"/api/research/status?album_ids={ALB1},{ALB2},{ALB3}")

        assert resp.status_code == 200
        assert resp.json()["statuses"] == {ALB1: "done", ALB2: "queued"}
        # the route forwards a parsed id list (blanks dropped) to the service,
        # now as UUID objects — status_map str()s them back for the map keys.
        assert svc.status_map.call_args.args[1] == [
            uuid.UUID(ALB1), uuid.UUID(ALB2), uuid.UUID(ALB3)
        ]
        app.dependency_overrides.clear()

    def test_too_many_ids_returns_400(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        resp = client.get("/api/research/status?album_ids=" + ",".join(str(uuid.uuid4()) for _ in range(501)))

        assert resp.status_code == 400
        svc.status_map.assert_not_called()
        app.dependency_overrides.clear()

    def test_missing_param_returns_422(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        resp = client.get("/api/research/status")

        assert resp.status_code == 422
        app.dependency_overrides.clear()


class TestTriggerResearch:
    def test_first_time_no_body_returns_200(self, client, app):
        svc = MagicMock()
        svc.trigger.return_value = _row(status="queued", result_md=None)
        _override(app, svc)

        resp = client.post(f"/api/research/albums/{ALB1}", json={})

        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"
        kwargs = svc.trigger.call_args.kwargs
        assert kwargs == {"mode": None, "instruction": None}
        app.dependency_overrides.clear()

    def test_refine_forwards_mode_and_instruction(self, client, app):
        svc = MagicMock()
        svc.trigger.return_value = _row(refine_count=1, last_instruction="샘플 더 조사")
        _override(app, svc)

        resp = client.post(
            f"/api/research/albums/{ALB1}",
            json={"mode": "refine", "instruction": "샘플 더 조사"},
        )

        assert resp.status_code == 200
        kwargs = svc.trigger.call_args.kwargs
        assert kwargs == {"mode": "refine", "instruction": "샘플 더 조사"}
        app.dependency_overrides.clear()

    def test_invalid_transition_returns_400(self, client, app):
        svc = MagicMock()
        svc.trigger.side_effect = ResearchStateError("refine is valid only on a completed note")
        _override(app, svc)

        resp = client.post(f"/api/research/albums/{ALB1}", json={"mode": "refine"})

        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_missing_album_returns_404(self, client, app):
        svc = MagicMock()
        svc.trigger.side_effect = AlbumNotFoundError("alb-x")
        _override(app, svc)

        resp = client.post(f"/api/research/albums/{ALB_MISSING}", json={})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_bad_mode_returns_422(self, client, app):
        _override(app, MagicMock())
        resp = client.post(f"/api/research/albums/{ALB1}", json={"mode": "bogus"})
        assert resp.status_code == 422
        app.dependency_overrides.clear()

    def test_post_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.post(f"/api/research/albums/{ALB1}", json={})

        assert resp.status_code == 401


# ── service transition logic (the RFC gates) ─────────────────────────────────────
# Mocks the DB boundary; asserts the row-state machine. The actual ON CONFLICT
# dedup is SQL — verified in the post-merge prod smoke.

def _svc_with(row, *, album_exists=True):
    svc = ResearchService()
    svc.get_research = MagicMock(return_value=row)
    svc._album_exists = MagicMock(return_value=album_exists)
    return svc


class TestTriggerLogic:
    def _db(self):
        return MagicMock()

    def test_restart_clears_note_and_requeues(self):
        row = _row(status="done", result_md="old", refine_count=2)
        svc = _svc_with(row)

        out = svc.trigger(self._db(), "alb-1", mode="restart")

        assert out.status == "queued"
        assert out.result_md is None  # restart clears the note
        assert out.error is None
        assert out.last_instruction is None

    def test_refine_keeps_note_and_increments(self):
        row = _row(status="done", result_md="좋은 노트", refine_count=1)
        svc = _svc_with(row)

        out = svc.trigger(self._db(), "alb-1", mode="refine", instruction="Y 출처 확인")

        assert out.status == "queued"
        assert out.result_md == "좋은 노트"  # refine NEVER clears the note
        assert out.refine_count == 2
        assert out.last_instruction == "Y 출처 확인"

    def test_refine_on_non_done_raises(self):
        row = _row(status="queued", result_md=None)
        svc = _svc_with(row)
        try:
            svc.trigger(self._db(), "alb-1", mode="refine", instruction="x")
            assert False, "expected ResearchStateError"
        except ResearchStateError:
            pass

    def test_refine_without_instruction_raises(self):
        row = _row(status="done", result_md="note")
        svc = _svc_with(row)
        try:
            svc.trigger(self._db(), "alb-1", mode="refine", instruction="  ")
            assert False, "expected ResearchStateError"
        except ResearchStateError:
            pass

    def test_plain_post_on_existing_row_is_noop(self):
        row = _row(status="done", result_md="note")
        svc = _svc_with(row)
        db = self._db()

        out = svc.trigger(db, "alb-1")  # no mode

        assert out is row              # returns existing state unchanged
        assert out.status == "done"    # not re-queued
        db.add.assert_not_called()     # no new row created

    def test_first_time_creates_queued_row(self):
        svc = _svc_with(None)          # no existing row
        db = self._db()

        out = svc.trigger(db, "alb-1")

        assert out.status == "queued"
        db.add.assert_called_once()

    def test_missing_album_raises(self):
        svc = _svc_with(None, album_exists=False)
        try:
            svc.trigger(self._db(), "ghost")
            assert False, "expected AlbumNotFoundError"
        except AlbumNotFoundError:
            pass


class TestEnqueueAlbum:
    def test_returns_true_on_insert(self):
        svc = ResearchService()
        db = MagicMock()
        db.execute.return_value = SimpleNamespace(rowcount=1)
        assert svc.enqueue_album(db, "alb-1") is True

    def test_returns_false_on_conflict(self):
        svc = ResearchService()
        db = MagicMock()
        db.execute.return_value = SimpleNamespace(rowcount=0)
        assert svc.enqueue_album(db, "alb-1") is False


class TestEnqueueBucketMixed:
    """FEAT-pocket-buckit Step 6 regression: after the V30/V31 relax a bucket can hold non-album
    rows (album_id=None). enqueue_bucket must skip them — else str(None)='None' hits the NOT-NULL
    album_research.album_id and silently aborts the rest of the enqueue loop. Exercises the REAL
    enqueue_bucket (the route tests mock it, so the loop itself was never covered on a mixed bucket)."""

    @staticmethod
    def _item(item_type, album_id, selected=False):
        return SimpleNamespace(
            item_type=item_type, album_id=album_id, research_selected=selected
        )

    def test_all_mode_enqueues_only_album_rows(self):
        svc = ResearchService()
        svc.enqueue_album = MagicMock(return_value=True)
        bucket = SimpleNamespace(research_mode="all", items=[
            self._item("album", "alb-1"),
            self._item("track", None),
            self._item("album", "alb-2"),
            self._item("snapshot", None),
            self._item("playback", None),
        ])
        n = svc.enqueue_bucket(MagicMock(), bucket)
        called = [c.args[1] for c in svc.enqueue_album.call_args_list]
        assert called == ["alb-1", "alb-2"]   # "None" never passed; both albums reached
        assert n == 2

    def test_selected_mode_enqueues_only_selected_album_rows(self):
        svc = ResearchService()
        svc.enqueue_album = MagicMock(return_value=True)
        bucket = SimpleNamespace(research_mode="selected", items=[
            self._item("album", "alb-1", selected=True),
            self._item("track", None, selected=True),     # selected but non-album → skipped
            self._item("album", "alb-2", selected=False),  # album but not selected → skipped
        ])
        n = svc.enqueue_bucket(MagicMock(), bucket)
        called = [c.args[1] for c in svc.enqueue_album.call_args_list]
        assert called == ["alb-1"]
        assert n == 1
