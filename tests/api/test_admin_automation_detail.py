"""FEAT-automation-admin 2f — owner queue detail.

No database here (the real SQL, including the reason-code cut, is
tests/integration/test_automation_db.py). This file pins:

* which keys resolve — only what the snapshot can show, the version only as a
  bind parameter;
* "read failed ⇒ unknown, with null sections", never an empty list;
* the session shape (read-only, SELECT-only, closed);
* the owner gate, which must answer before the unknown-key 404 does.
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
    DetailRead,
    assemble_detail,
    resolve_queue,
)

NOW = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
GPT_KEY = catalog.translation_queue_key(catalog.GPT_LYRICS_VERSION)
CLAUDE_KEY = catalog.translation_queue_key(catalog.CLAUDE_DEMAND_VERSION)


# ── catalog / key resolution ────────────────────────────────────────────────────


class TestResolve:
    def test_every_snapshot_queue_has_a_detail(self):
        assert set(catalog.QUEUE_DETAILS) == {qd.key for qd in catalog.QUEUES}
        for qd in catalog.QUEUES:
            assert resolve_queue(qd.key) is not None, qd.key

    def test_known_translation_versions_keep_their_label_and_freeze(self):
        t = resolve_queue(CLAUDE_KEY)
        assert t is not None
        assert t.frozen is True
        assert t.label == "Claude demand translation (legacy)"
        assert t.params == {"version": catalog.CLAUDE_DEMAND_VERSION}
        assert t.definition is catalog.TRANSLATION_WORK_DETAIL

    def test_an_uncatalogued_version_still_resolves(self):
        # The snapshot lists versions the catalog has not heard of; their detail
        # must open too, or the panel would 404 on a row the page itself shows.
        t = resolve_queue("lyrics_translation_work:it/new-v9")
        assert t is not None
        assert t.params == {"version": "it/new-v9"}
        assert t.frozen is False

    @pytest.mark.parametrize("key", [
        "",
        "tracks",
        "automation_heartbeats",
        "lyrics_album_jobs ",
        "LYRICS_ALBUM_JOBS",
        "lyrics_translation_work",
        "lyrics_translation_work:",
        "lyrics_translation_work:   ",
        "lyrics_translation_work:" + "v" * (catalog.MAX_TRANSLATOR_VERSION_LENGTH + 1),
        "lyrics_album_jobs; DROP TABLE posts",
    ])
    def test_anything_else_is_unknown(self, key):
        assert resolve_queue(key) is None

    def test_version_reaches_sql_only_as_a_bind_parameter(self):
        hostile = "x' OR '1'='1"
        t = resolve_queue(f"lyrics_translation_work:{hostile}")
        assert t is not None
        d = t.definition
        for sql in (d.reasons_sql, d.flow_sql, d.oldest_sql):
            assert hostile not in sql
            assert ":version" in sql

    def test_no_detail_sql_selects_a_raw_error_column(self):
        # `error` columns hold raw exception text; reasons come only from
        # `last_reason`, and only through reason_code_sql.
        defs = list(catalog.QUEUE_DETAILS.values()) + [catalog.TRANSLATION_WORK_DETAIL]
        wrapped = [catalog.reason_code_sql(c) for c in ("last_reason", "j.last_reason", "lt.last_reason",
                                                         "d.last_reason", "w.last_reason")]
        for d in defs:
            for sql in (d.reasons_sql, d.oldest_sql):
                assert not re.search(r"\berror\b", sql), sql
                # Every mention of last_reason sits inside a reason_code_sql CASE.
                for w in wrapped:
                    sql = sql.replace(w, "<code>")
                assert "last_reason" not in sql, sql

    def test_reasonless_tables_say_so(self):
        for key in ("album_research", "genre_backfill_requests",
                    "track_lyrics_translations", "track_genius_annotations"):
            assert catalog.QUEUE_DETAILS[key].reasons_note, key
        for key in ("lyrics_album_jobs", "lyrics_album_tracks", "lyrics_artist_discographies"):
            assert catalog.QUEUE_DETAILS[key].reasons_note is None, key
        assert catalog.TRANSLATION_WORK_DETAIL.reasons_note is None


# ── assembly ────────────────────────────────────────────────────────────────────


class TestAssemble:
    def test_failed_read_is_unknown_with_null_sections(self):
        t = resolve_queue("lyrics_album_jobs")
        d = assemble_detail(t, None)
        assert d.status == "unknown"
        assert d.reasons is None and d.flow is None and d.oldest_waiting is None
        assert d.observed_at is None
        # The static parts are still there to explain the panel.
        assert d.wait_basis and d.oldest_limit == catalog.OLDEST_WAITING_LIMIT

    def test_empty_read_is_ok_with_empty_lists(self):
        t = resolve_queue("album_research")
        d = assemble_detail(t, DetailRead(NOW, [], (0, 0), []))
        assert d.status == "ok"
        assert d.reasons == [] and d.oldest_waiting == []
        assert d.flow.entered == 0 and d.flow.left == 0
        assert d.reasons_note == catalog.QUEUE_DETAILS["album_research"].reasons_note

    def test_reasons_sorted_and_nulls_kept(self):
        t = resolve_queue("lyrics_album_tracks")
        read = DetailRead(
            NOW,
            [("not_required", "no_lyrics", 118, 0),
             ("source_pending", "not_found", 2409, 2409),
             ("linked", None, 40, 0)],
            (None, 285),
            [("t-1", "MUSIC", "source_pending", "not_found", None,
              NOW - timedelta(days=16), NOW + timedelta(hours=5))],
        )
        d = assemble_detail(t, read)
        assert [(r.state, r.reason, r.count, r.deferred) for r in d.reasons] == [
            ("source_pending", "not_found", 2409, 2409),
            ("not_required", "no_lyrics", 118, 0),
            ("linked", None, 40, 0),
        ]
        # Not measurable stays null, it does not become 0.
        assert d.flow.entered is None and d.flow.left == 285
        assert "created_at" in d.flow.entered_basis
        item = d.oldest_waiting[0]
        assert (item.id, item.title, item.reason, item.attempts) == ("t-1", "MUSIC", "not_found", None)
        assert item.next_attempt_at == NOW + timedelta(hours=5)


# ── service session ─────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def scalar_one(self):
        return self._rows[0][0]


class _FakeSession:
    def __init__(self, fail_on=None):
        self.statements: list[tuple[str, dict]] = []
        self.closed = False
        self._fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.statements.append((sql, dict(params or {})))
        if self._fail_on and self._fail_on in sql:
            raise OperationalError(sql, {}, Exception("boom"))
        if sql == "SELECT now()":
            return _FakeResult([(NOW,)])
        if "LIMIT :limit" in sql or "GROUP BY" in sql:
            return _FakeResult([])
        return _FakeResult([(0, 0)])  # flow


class TestServiceSession:
    def test_read_only_select_only_closed_and_bound(self):
        session = _FakeSession()
        d = AutomationService(lambda: session).queue_detail(GPT_KEY)

        assert d is not None and d.status == "ok"
        assert session.statements[0][0] == "SET TRANSACTION READ ONLY"
        assert all(s.startswith("SELECT") for s, _ in session.statements[1:]), session.statements
        assert session.closed
        bound = [p for s, p in session.statements if "lyrics_translation_work" in s]
        assert len(bound) == 3
        assert all(p.get("version") == catalog.GPT_LYRICS_VERSION for p in bound)
        assert [p["limit"] for s, p in session.statements if "LIMIT :limit" in s] == [catalog.OLDEST_WAITING_LIMIT]

    def test_unknown_key_opens_no_session(self):
        opened = []
        assert AutomationService(lambda: opened.append(1)).queue_detail("tracks") is None
        assert opened == []

    def test_db_error_degrades_to_unknown(self):
        session = _FakeSession(fail_on="LIMIT :limit")
        d = AutomationService(lambda: session).queue_detail("lyrics_album_jobs")
        assert session.closed
        # A partial read (reasons and flow succeeded) is discarded, not half-shown.
        assert d.status == "unknown"
        assert d.reasons is None and d.flow is None and d.oldest_waiting is None


# ── route: owner gate ───────────────────────────────────────────────────────────

PATH = "/api/admin/automation/queue-detail"


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
    s.queue_detail.side_effect = lambda key: (
        None if resolve_queue(key) is None else assemble_detail(resolve_queue(key), None)
    )
    app.dependency_overrides[get_automation_service] = lambda: s
    return s


def _as(app, sub):
    from app.core.auth import require_cognito_token

    app.dependency_overrides[require_cognito_token] = lambda: {"sub": sub}


class TestRoute:
    def test_local_returns_the_detail(self, client, svc):
        resp = client.get(PATH, params={"key": GPT_KEY})
        assert resp.status_code == 200
        body = resp.json()
        assert body["key"] == GPT_KEY and body["status"] == "unknown"
        svc.queue_detail.assert_called_once_with(GPT_KEY)

    def test_unknown_key_is_404(self, client, svc):
        resp = client.get(PATH, params={"key": "tracks"})
        assert resp.status_code == 404

    def test_missing_key_is_422_for_the_owner(self, client, svc):
        assert client.get(PATH).status_code == 422
        svc.queue_detail.assert_not_called()

    def test_missing_token_is_401_in_prod(self, client, svc):
        import app.core.auth as auth_module

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(PATH, params={"key": GPT_KEY})
        assert resp.status_code == 401
        svc.queue_detail.assert_not_called()

    @pytest.mark.parametrize("params", [{"key": GPT_KEY}, {"key": "tracks"}, {}])
    def test_non_owner_is_403_before_any_key_handling(self, client, app, svc, params):
        # 403 for a real key, an unknown key and a missing key alike: a member
        # cannot learn which queue keys exist from 404-vs-200 or 422.
        import app.core.auth as auth_module

        _as(app, "some-member")
        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(PATH, params=params)
        assert resp.status_code == 403
        svc.queue_detail.assert_not_called()

    def test_owner_reaches_the_service_in_prod(self, client, app, svc):
        import app.core.auth as auth_module

        _as(app, "owner-sub")
        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(PATH, params={"key": CLAUDE_KEY})
        assert resp.status_code == 200
        assert resp.json()["frozen"] is True
