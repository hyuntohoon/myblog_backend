"""FEAT-lyrics-match-correction Step 3a — the owner's decisions against real Postgres.

The route suite mocks the service. What only a real engine shows: that each decision
commits what it claims (the V33 CHECKs accept a manual no_lyrics and a taken-down row),
that a rejection is idempotent through ON CONFLICT, that the row lock does not deadlock a
read-then-write, and that every SQS send happens after the commit (nothing held open).

V63 must already be applied — asserted, never created here.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.lyrics_admin_service import (
    LyricsAdminConflict,
    LyricsAdminService,
    LyricsAdminTrackNotFound,
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
        for t in ("track_lyrics_candidates", "track_lyrics_rejections"):
            assert c.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None, f"V63 missing: {t}"
    yield sessionmaker(bind=eng)
    eng.dispose()


class _Sqs:
    """Records sends, and the DB transaction state at the moment of each send."""

    def __init__(self, session, ok=True):
        self.session, self.ok, self.sent = session, ok, []

    def send_lyrics_recheck(self, track_id):
        self.sent.append(("recheck", track_id, self.session.in_transaction()))
        return self.ok

    def send_lyrics_pick(self, track_id, lrclib_id, actor):
        self.sent.append(("pick", track_id, lrclib_id, actor, self.session.in_transaction()))
        return self.ok


@pytest.fixture
def track(factory):
    album, tid, artist = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sid = "lmc3a" + uuid.uuid4().hex[:17]
    s = factory()
    s.execute(text("INSERT INTO albums (id, spotify_id, title) VALUES (:id, :sid, 'LMC 3a')"),
              {"id": album, "sid": "lmc3a-al-" + uuid.uuid4().hex[:12]})
    s.execute(text("INSERT INTO tracks (id, album_id, spotify_id, title, duration_sec) "
                   "VALUES (:id, :al, :sid, 'Bad Habit', 232)"), {"id": tid, "al": album, "sid": sid})
    s.execute(text("INSERT INTO artists (id, spotify_id, name) VALUES (:id, :sid, 'Steve Lacy')"),
              {"id": artist, "sid": "lmc3a-ar-" + uuid.uuid4().hex[:12]})
    s.execute(text("INSERT INTO track_artists (track_id, artist_id) VALUES (:t, :a)"), {"t": tid, "a": artist})
    s.commit()
    s.close()
    yield sid, tid
    s = factory()
    s.execute(text("DELETE FROM albums WHERE id = :id"), {"id": album})
    s.execute(text("DELETE FROM artists WHERE id = :id"), {"id": artist})
    s.commit()
    s.close()


def _seed(factory, tid, *, status="matched", evidence=None, body="I wish I knew you",
          candidates=(), fetched_at=None):
    s = factory()
    s.execute(
        text("INSERT INTO track_lyrics (track_id, match_status, evidence, lyric_plain, matcher_version) "
             "VALUES (:t, :st, CAST(:ev AS jsonb), :body, 'step4-v1')"),
        {"t": tid, "st": status, "ev": json.dumps(evidence or {}), "body": body},
    )
    for lid, verdict, cluster in candidates:
        s.execute(
            text("INSERT INTO track_lyrics_candidates (track_id, lrclib_id, title, artist, preview, "
                 "cluster_size, verdict, matcher_version, fetched_at) "
                 "VALUES (:t, :l, 'Bad Habit', 'Steve Lacy', 'I wish I knew you', :c, :v, 'step4-v1', "
                 "COALESCE(CAST(:f AS timestamptz), now()))"),
            {"t": tid, "l": lid, "v": verdict, "c": cluster, "f": fetched_at},
        )
    s.commit()
    s.close()


def _row(factory, tid):
    s = factory()
    try:
        return s.execute(text("SELECT match_status, evidence, lyric_plain, lyric_synced, matcher_version "
                              "FROM track_lyrics WHERE track_id = :t"), {"t": tid}).one_or_none()
    finally:
        s.close()


def _decide(factory, sid, action, lrclib_id=None, ok=True):
    s = factory()
    sqs = _Sqs(s, ok)
    try:
        res = LyricsAdminService(sqs).decide(s, spotify_track_id=sid, action=action,
                                             lrclib_id=lrclib_id, actor="owner-sub")
        assert not s.in_transaction(), "a decision left its transaction open"
        return res, sqs.sent
    finally:
        s.close()


GOOD, WRONG = 34738767, 8996695


class TestRead:
    def test_sorted_by_verdict_with_current_and_rejected_flags(self, factory, track):
        sid, tid = track
        _seed(factory, tid, evidence={"match_basis": "exact-title", "lrclib_id": WRONG},
              candidates=[(WRONG, "outvoted", 1), (GOOD, "picked", 19), (777, "artist_mismatch", None)])
        s = factory()
        s.execute(text("INSERT INTO track_lyrics_rejections (track_id, lrclib_id, rejected_by) "
                       "VALUES (:t, 777, 'owner-sub'), (:t, 999, 'owner-sub')"), {"t": tid})
        s.commit()
        out = LyricsAdminService().candidates(s, spotify_track_id=sid)
        s.close()
        assert [c.lrclib_id for c in out.candidates] == [GOOD, WRONG, 777]
        assert [c.current for c in out.candidates] == [False, True, False]
        assert out.candidates[2].verdict == "rejected" and out.candidates[2].rejected
        assert out.rejected_lrclib_ids == [777, 999]
        assert out.track_artists == ["Steve Lacy"] and out.current.lrclib_id == WRONG
        assert out.stale is False

    @pytest.mark.parametrize("case", ["nothing_stored", "old", "served_not_in_set"])
    def test_stale(self, factory, track, case):
        sid, tid = track
        if case == "nothing_stored":
            _seed(factory, tid, evidence={"lrclib_id": GOOD})
        elif case == "old":
            _seed(factory, tid, evidence={"lrclib_id": GOOD}, candidates=[(GOOD, "picked", 2)],
                  fetched_at=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
        else:
            _seed(factory, tid, evidence={"lrclib_id": WRONG}, candidates=[(GOOD, "picked", 2)])
        s = factory()
        assert LyricsAdminService().candidates(s, spotify_track_id=sid).stale is True
        s.close()

    def test_unknown_track(self, factory):
        s = factory()
        with pytest.raises(LyricsAdminTrackNotFound):
            LyricsAdminService().candidates(s, spotify_track_id="nope-" + uuid.uuid4().hex)
        s.close()


class TestDecisions:
    def test_no_lyrics_locks_and_satisfies_v33(self, factory, track):
        sid, tid = track
        _seed(factory, tid, evidence={"match_basis": "exact-title", "lrclib_id": WRONG})
        res, sent = _decide(factory, sid, "no_lyrics")
        assert (res.outcome, res.current.lock, res.current.match_status) == ("applied", "manual", "no_lyrics")
        status, ev, plain, synced, version = _row(factory, tid)
        assert (status, plain, synced, version) == ("no_lyrics", "", None, "manual")
        assert ev["match_basis"] == "manual" and ev["manual"]["previous"]["lrclib_id"] == WRONG
        assert sent == []

    def test_no_lyrics_on_a_never_evaluated_track_inserts(self, factory, track):
        sid, tid = track
        res, _ = _decide(factory, sid, "no_lyrics")
        assert res.current.lock == "manual" and _row(factory, tid)[0] == "no_lyrics"

    def test_rejecting_the_served_lyric_takes_it_down_and_rechecks(self, factory, track):
        sid, tid = track
        _seed(factory, tid, evidence={"match_basis": "manual", "lrclib_id": WRONG})  # even locked
        res, sent = _decide(factory, sid, "reject", WRONG)
        assert (res.outcome, res.recheck_enqueued) == ("applied", True)
        assert res.current.match_status == "review_required" and res.current.lock is None
        assert res.current.reason == "owner_rejected"
        status, ev, plain, synced, _ = _row(factory, tid)
        assert (status, plain, synced) == ("review_required", None, None)
        assert ev["rejected_lrclib_id"] == WRONG
        assert sent == [("recheck", str(tid), False)]  # sent after the commit

    def test_rejecting_another_candidate_only_records_it_and_is_idempotent(self, factory, track):
        sid, tid = track
        _seed(factory, tid, evidence={"match_basis": "exact-title", "lrclib_id": GOOD})
        first, sent1 = _decide(factory, sid, "reject", WRONG)
        again, sent2 = _decide(factory, sid, "reject", WRONG)
        assert (first.outcome, again.outcome) == ("applied", "no_change")
        assert sent1 == sent2 == []
        assert _row(factory, tid)[2] == "I wish I knew you"  # still served

    def test_unlock_hands_back_and_rechecks(self, factory, track):
        sid, tid = track
        _seed(factory, tid, evidence={"match_basis": "exact-title", "lrclib_id": GOOD,
                                      "manual_fix": {"approved_by": "owner"}})
        res, sent = _decide(factory, sid, "unlock")
        assert (res.outcome, res.current.lock, res.recheck_enqueued) == ("applied", None, True)
        ev = _row(factory, tid)[1]
        assert "manual_fix" not in ev and ev["match_basis"] == "unlocked"
        assert ev["unlocked"]["previous"]["lock"] == "manual_fix"
        assert sent == [("recheck", str(tid), False)]
        again, _ = _decide(factory, sid, "unlock")
        assert again.outcome == "no_change"

    def test_pick_is_validated_against_the_stored_set_then_enqueued(self, factory, track):
        sid, tid = track
        _seed(factory, tid, evidence={"lrclib_id": WRONG}, candidates=[(GOOD, "picked", 19), (WRONG, "outvoted", 1)])
        with pytest.raises(LyricsAdminConflict) as exc:
            _decide(factory, sid, "pick", 12345)
        assert exc.value.code == "not_a_candidate"
        _decide(factory, sid, "reject", WRONG)
        with pytest.raises(LyricsAdminConflict) as exc:
            _decide(factory, sid, "pick", WRONG)
        assert exc.value.code == "rejected"
        res, sent = _decide(factory, sid, "pick", GOOD)
        assert res.outcome == "enqueued"
        assert sent == [("pick", str(tid), GOOD, "owner-sub", False)]
        assert _row(factory, tid)[0] == "review_required"  # the worker writes the pick
