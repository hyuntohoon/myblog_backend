"""FEAT-lyrics-match-correction Step 3b — member lyrics reports against real Postgres.

What only a real engine shows: the V64 partial unique through ON CONFLICT (a repeat is
`already_reported` and burns no cap), the rolling-24h cap, that only the track's first open
report enqueues the re-check (and with the member cooldown), that every send happens after
the commit, the derived `auto_resolved` state, and which owner decisions close reports.

V64 must already be applied — asserted, never created here.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.services.lyrics_admin_service import LyricsAdminQueueUnavailable, LyricsAdminService
from app.services.lyrics_report_service import (
    MEMBER_RECHECK_COOLDOWN_SEC,
    _advisory_key,
    LyricsReportNotReportable,
    LyricsReportRateLimited,
    LyricsReportService,
    LyricsReportTrackNotFound,
)

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]


@pytest.fixture(scope="module")
def factory():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True, pool_size=2)
    with eng.connect() as c:
        assert c.execute(text("SELECT to_regclass('track_lyrics_reports')")).scalar() is not None, \
            "V64 missing: track_lyrics_reports"
    yield sessionmaker(bind=eng)
    eng.dispose()


class _Sqs:
    """Records sends, and the DB transaction state at the moment of each send."""

    def __init__(self, session, ok=True):
        self.session, self.ok, self.sent = session, ok, []

    def send_lyrics_recheck(self, track_id, cooldown_sec=0):
        self.sent.append(("recheck", track_id, cooldown_sec, self.session.in_transaction()))
        return self.ok

    def send_lyrics_pick(self, track_id, lrclib_id, actor):
        self.sent.append(("pick", track_id, lrclib_id, actor, self.session.in_transaction()))
        return self.ok


@pytest.fixture
def members(factory):
    ids = [uuid.uuid4() for _ in range(3)]
    s = factory()
    for u in ids:
        s.execute(text("INSERT INTO users (id, handle, display_name) VALUES (:id, :h, 'LMC 3b')"),
                  {"id": u, "h": "lmc3b_" + u.hex[:20]})
    s.commit()
    s.close()
    yield ids
    s = factory()
    s.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids})
    s.commit()
    s.close()


def _make_track(factory, title="Bad Habit"):
    album, tid, artist = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sid = "lmc3b" + uuid.uuid4().hex[:17]
    s = factory()
    s.execute(text("INSERT INTO albums (id, spotify_id, title) VALUES (:id, :sid, 'LMC 3b')"),
              {"id": album, "sid": "lmc3b-al-" + uuid.uuid4().hex[:12]})
    s.execute(text("INSERT INTO tracks (id, album_id, spotify_id, title, duration_sec) "
                   "VALUES (:id, :al, :sid, :title, 232)"), {"id": tid, "al": album, "sid": sid, "title": title})
    s.execute(text("INSERT INTO artists (id, spotify_id, name) VALUES (:id, :sid, 'Steve Lacy')"),
              {"id": artist, "sid": "lmc3b-ar-" + uuid.uuid4().hex[:12]})
    s.execute(text("INSERT INTO track_artists (track_id, artist_id) VALUES (:t, :a)"), {"t": tid, "a": artist})
    s.commit()
    s.close()
    return sid, tid, album, artist


@pytest.fixture
def tracks(factory):
    made = []

    def make(**kw):
        made.append(_make_track(factory, **kw))
        return made[-1][:2]

    yield make
    s = factory()
    for _, _, album, artist in made:
        s.execute(text("DELETE FROM albums WHERE id = :id"), {"id": album})
        s.execute(text("DELETE FROM artists WHERE id = :id"), {"id": artist})
    s.commit()
    s.close()


def _seed(factory, tid, *, status="matched", lrclib_id=34738767, body="I wish I knew you"):
    s = factory()
    s.execute(
        text("INSERT INTO track_lyrics (track_id, match_status, evidence, lyric_plain, matcher_version) "
             "VALUES (:t, :st, CAST(:ev AS jsonb), :body, 'step4-v1')"),
        {"t": tid, "st": status, "ev": json.dumps({"lrclib_id": lrclib_id, "match_basis": "exact-title"}),
         "body": body if status == "matched" else ("" if status == "no_lyrics" else None)},
    )
    s.commit()
    s.close()


def _report(factory, sid, member, cap=20, ok=True):
    s = factory()
    sqs = _Sqs(s, ok)
    try:
        res = LyricsReportService(sqs).report(s, spotify_track_id=sid, member_id=member, daily_cap=cap)
        assert not s.in_transaction(), "a report left its transaction open"
        return res, sqs.sent
    finally:
        s.close()


def _reports(factory, tid):
    s = factory()
    try:
        return s.execute(text("SELECT user_id, status, resolution, reported_status, reported_lrclib_id, "
                              "reported_body_sha FROM track_lyrics_reports WHERE track_id = :t "
                              "ORDER BY created_at"), {"t": tid}).all()
    finally:
        s.close()


def _queue(factory):
    s = factory()
    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = s.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        res = LyricsReportService(_Sqs(s)).queue(s)
        assert not s.in_transaction(), "the queue read left a transaction open"
        # A fixed number of bulk reads (count, top, reports, tracks, rows, artists),
        # whatever the queue size: the response is built before the rollback, so no
        # expired object is reloaded one SELECT at a time.
        assert len(statements) <= 6, f"{len(statements)} statements for the queue"
        return res
    finally:
        event.remove(engine, "before_cursor_execute", count)
        s.close()


def _decide(factory, sid, action, lrclib_id=None, ok=True):
    s = factory()
    sqs = _Sqs(s, ok)
    try:
        return LyricsAdminService(sqs).decide(s, spotify_track_id=sid, action=action,
                                              lrclib_id=lrclib_id, actor="owner-sub"), sqs.sent
    finally:
        s.close()


class TestMemberReport:
    def test_first_report_snapshots_and_rechecks_after_commit(self, factory, tracks, members):
        sid, tid = tracks()
        _seed(factory, tid)
        res, sent = _report(factory, sid, members[0])
        assert res.outcome == "reported" and res.recheck_enqueued
        assert sent == [("recheck", str(tid), MEMBER_RECHECK_COOLDOWN_SEC, False)]
        (row,) = _reports(factory, tid)
        assert row.user_id == members[0] and row.status == "open" and row.resolution is None
        assert row.reported_status == "matched" and row.reported_lrclib_id == 34738767
        assert len(row.reported_body_sha) == 64
        # Never touches track_lyrics.
        s = factory()
        assert s.execute(text("SELECT match_status, lyric_plain FROM track_lyrics WHERE track_id=:t"),
                         {"t": tid}).one() == ("matched", "I wish I knew you")
        s.close()

    def test_repeat_is_already_reported_without_cap_or_recheck(self, factory, tracks, members):
        sid, tid = tracks()
        _seed(factory, tid)
        _report(factory, sid, members[0])
        res, sent = _report(factory, sid, members[0], cap=1)  # cap already spent: still no 429
        assert res.outcome == "already_reported" and sent == []
        assert len(_reports(factory, tid)) == 1

    def test_only_the_first_open_report_rechecks(self, factory, tracks, members):
        sid, tid = tracks()
        _seed(factory, tid)
        _report(factory, sid, members[0])
        res, sent = _report(factory, sid, members[1])
        assert res.outcome == "reported" and sent == []
        assert len(_reports(factory, tid)) == 2

    def test_concurrent_first_reporters_send_one_recheck(self, factory, tracks, members):
        """The advisory lock, not luck: another session's not-yet-committed first report
        holds the track's lock; this report waits for it and then sees it, so only one
        re-check goes out. Without the lock this report reads no open report and sends."""
        sid, tid = tracks()
        _seed(factory, tid)
        holder = factory()
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key("track", tid)})
        holder.execute(text("INSERT INTO track_lyrics_reports (track_id, user_id) VALUES (:t, :u)"),
                       {"t": tid, "u": members[0]})
        out = {}
        worker = threading.Thread(target=lambda: out.update(r=_report(factory, sid, members[1])))
        worker.start()
        time.sleep(1.5)
        assert worker.is_alive(), "the report did not wait for the track's lock"
        holder.commit()
        holder.close()
        worker.join(30)
        res, sent = out["r"]
        assert res.outcome == "reported" and sent == []
        assert len(_reports(factory, tid)) == 2

    def test_concurrent_reports_by_one_member_share_the_cap(self, factory, tracks, members):
        """The member's lock, not luck: with one report left in the budget, a report on
        another track waits for the member's uncommitted report and then sees the cap
        spent. Keyed on the track alone, both would read the count before either commits."""
        (sid_a, tid_a), (sid_b, tid_b) = tracks(), tracks()
        _seed(factory, tid_a)
        _seed(factory, tid_b)
        holder = factory()
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key("member", members[0])})
        holder.execute(text("INSERT INTO track_lyrics_reports (track_id, user_id) VALUES (:t, :u)"),
                       {"t": tid_a, "u": members[0]})
        out = {}

        def run():
            try:
                out["r"] = _report(factory, sid_b, members[0], cap=1)
            except Exception as error:  # noqa: BLE001 - asserted below
                out["e"] = error

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(1.5)
        assert worker.is_alive(), "the report did not wait for the member's lock"
        holder.commit()
        holder.close()
        worker.join(30)
        assert isinstance(out.get("e"), LyricsReportRateLimited), out
        assert _reports(factory, tid_b) == []

    def test_no_lyrics_is_reportable(self, factory, tracks, members):
        sid, tid = tracks()
        _seed(factory, tid, status="no_lyrics")
        res, _ = _report(factory, sid, members[0])
        assert res.outcome == "reported"
        (row,) = _reports(factory, tid)
        assert row.reported_status == "no_lyrics" and row.reported_body_sha is None

    @pytest.mark.parametrize("status", [None, "review_required", "matched-empty"])
    def test_what_the_viewer_does_not_show_is_not_reportable(self, factory, tracks, members, status):
        sid, tid = tracks()
        if status == "matched-empty":
            _seed(factory, tid, body="  ")
        elif status:
            _seed(factory, tid, status=status)
        with pytest.raises(LyricsReportNotReportable):
            _report(factory, sid, members[0])
        assert _reports(factory, tid) == []

    def test_unknown_track(self, factory, members):
        with pytest.raises(LyricsReportTrackNotFound):
            _report(factory, "lmc3b-nope-" + uuid.uuid4().hex[:8], members[0])

    def test_daily_cap_counts_resolved_reports_too(self, factory, tracks, members):
        made = []
        for _ in range(3):
            sid, tid = tracks()
            _seed(factory, tid)
            made.append((sid, tid))
        _report(factory, made[0][0], members[0], cap=2)
        _decide(factory, made[0][0], "keep")  # resolved: still counts
        _report(factory, made[1][0], members[0], cap=2)
        with pytest.raises(LyricsReportRateLimited):
            _report(factory, made[2][0], members[0], cap=2)
        assert _reports(factory, made[2][1]) == []
        # Another member has their own budget.
        assert _report(factory, made[2][0], members[1], cap=2)[0].outcome == "reported"

    def test_send_failure_keeps_the_report(self, factory, tracks, members):
        sid, tid = tracks()
        _seed(factory, tid)
        res, _ = _report(factory, sid, members[0], ok=False)
        assert res.outcome == "reported" and not res.recheck_enqueued
        assert len(_reports(factory, tid)) == 1


class TestOwnerQueue:
    def _item(self, factory, sid):
        return next((i for i in _queue(factory).items if i.spotify_track_id == sid), None)

    def test_grouped_open_until_served_body_changes(self, factory, tracks, members):
        sid, tid = tracks()
        _seed(factory, tid)
        _report(factory, sid, members[0])
        _report(factory, sid, members[1])
        item = self._item(factory, sid)
        assert item.report_count == 2 and item.member_count == 2 and item.state == "open"
        assert item.reported.lrclib_id == 34738767 and item.track_artists == ["Steve Lacy"]
        # Same body under a new LRCLIB id is not a change the member would see.
        s = factory()
        s.execute(text("UPDATE track_lyrics SET evidence = jsonb_set(evidence, '{lrclib_id}', '999') "
                       "WHERE track_id=:t"), {"t": tid})
        s.commit()
        assert self._item(factory, sid).state == "open"
        s.execute(text("UPDATE track_lyrics SET lyric_plain='The right words' WHERE track_id=:t"), {"t": tid})
        s.commit()
        s.close()
        assert self._item(factory, sid).state == "auto_resolved"

    def test_taken_down_row_reads_auto_resolved(self, factory, tracks, members):
        sid, tid = tracks()
        _seed(factory, tid)
        _report(factory, sid, members[0])
        s = factory()
        s.execute(text("UPDATE track_lyrics SET match_status='review_required', lyric_plain=NULL "
                       "WHERE track_id=:t"), {"t": tid})
        s.commit()
        s.close()
        assert self._item(factory, sid).state == "auto_resolved"


class TestDecisionsCloseReports:
    def _open(self, factory, tracks, members, n=2):
        sid, tid = tracks()
        _seed(factory, tid)
        for m in members[:n]:
            _report(factory, sid, m)
        return sid, tid

    def test_keep_closes_and_changes_nothing_else(self, factory, tracks, members):
        sid, tid = self._open(factory, tracks, members)
        res, sent = _decide(factory, sid, "keep")
        assert res.action == "keep" and res.outcome == "applied" and res.reports_resolved == 2
        assert sent == [] and res.current.lock is None
        assert {(r.status, r.resolution) for r in _reports(factory, tid)} == {("resolved", "keep")}
        s = factory()
        ev = s.execute(text("SELECT evidence FROM track_lyrics WHERE track_id=:t"), {"t": tid}).scalar()
        s.close()
        assert ev == {"lrclib_id": 34738767, "match_basis": "exact-title"}
        again, _ = _decide(factory, sid, "keep")
        assert again.outcome == "no_change" and again.reports_resolved == 0

    def test_keep_never_unlocks(self, factory, tracks, members):
        sid, tid = self._open(factory, tracks, members, n=1)
        s = factory()
        s.execute(text("UPDATE track_lyrics SET evidence = evidence || '{\"manual_fix\": {\"by\": \"owner\"}}' "
                       "WHERE track_id=:t"), {"t": tid})
        s.commit()
        s.close()
        res, sent = _decide(factory, sid, "keep")
        assert res.current.lock == "manual_fix" and sent == []

    def test_no_lyrics_closes(self, factory, tracks, members):
        sid, tid = self._open(factory, tracks, members)
        res, _ = _decide(factory, sid, "no_lyrics")
        assert res.reports_resolved == 2
        assert {r.resolution for r in _reports(factory, tid)} == {"no_lyrics"}

    def test_reject_closes_only_when_it_takes_the_served_lyric_down(self, factory, tracks, members):
        sid, tid = self._open(factory, tracks, members)
        other, _ = _decide(factory, sid, "reject", 111)
        assert other.reports_resolved == 0
        assert {r.status for r in _reports(factory, tid)} == {"open"}
        served, _ = _decide(factory, sid, "reject", 34738767)
        assert served.reports_resolved == 2
        assert {r.resolution for r in _reports(factory, tid)} == {"reject"}

    def test_unlock_closes_nothing(self, factory, tracks, members):
        sid, tid = self._open(factory, tracks, members)
        s = factory()
        s.execute(text("UPDATE track_lyrics SET evidence = evidence || '{\"match_basis\": \"manual\"}' "
                       "WHERE track_id=:t"), {"t": tid})
        s.commit()
        s.close()
        res, _ = _decide(factory, sid, "unlock")
        assert res.outcome == "applied" and res.reports_resolved == 0
        assert {r.status for r in _reports(factory, tid)} == {"open"}

    def test_pick_closes_after_the_send(self, factory, tracks, members):
        sid, tid = self._open(factory, tracks, members)
        s = factory()
        s.execute(text("INSERT INTO track_lyrics_candidates (track_id, lrclib_id, title, artist, verdict, "
                       "matcher_version) VALUES (:t, 8996695, 'Bad Habit', 'Steve Lacy', 'outvoted', 'step4-v1')"),
                  {"t": tid})
        s.commit()
        s.close()
        res, sent = _decide(factory, sid, "pick", 8996695)
        assert res.outcome == "enqueued" and res.reports_resolved == 2
        assert sent[0][0] == "pick" and sent[0][-1] is False
        assert {r.resolution for r in _reports(factory, tid)} == {"pick"}
        # A member may report it again once closed.
        assert _report(factory, sid, members[0])[0].outcome == "reported"

    def test_pick_whose_send_fails_closes_nothing(self, factory, tracks, members):
        sid, tid = self._open(factory, tracks, members)
        s = factory()
        s.execute(text("INSERT INTO track_lyrics_candidates (track_id, lrclib_id, title, artist, verdict, "
                       "matcher_version) VALUES (:t, 8996695, 'Bad Habit', 'Steve Lacy', 'outvoted', 'step4-v1')"),
                  {"t": tid})
        s.commit()
        s.close()
        with pytest.raises(LyricsAdminQueueUnavailable):
            _decide(factory, sid, "pick", 8996695, ok=False)
        assert {r.status for r in _reports(factory, tid)} == {"open"}
