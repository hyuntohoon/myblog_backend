"""FEAT-lyrics-translation Step 2 units — fingerprint, additive translation view,
and the request-translation upsert (mock-db wiring, mirroring TestGetNormalized).

The fingerprint contract matters most: the poller imports compute_source_fingerprint
from this repo's checkout, and read-time staleness is re-derived from it — a silent
shape change here would flip every stored translation to "stale".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.lyrics_service import (
    LyricsNotTranslatableError,
    LyricsService,
    LyricsTrackNotFoundError,
    attach_translation,
    compute_source_fingerprint,
    normalize_lyrics,
)

_TRACK_ID = "11111111-1111-1111-1111-111111111111"


def _lyrics_row(lyric_plain="line one\n\nline two", lyric_synced=None, match_status="matched"):
    return SimpleNamespace(
        track_id=_TRACK_ID,
        match_status=match_status,
        lyric_plain=lyric_plain,
        lyric_synced=lyric_synced,
    )


def _ok_payload():
    # 3 segments: "line one", "" (gap), "line two"
    return normalize_lyrics(_lyrics_row())


def _done_row(payload, *, origin="poller", lang="ko", **overrides):
    row = SimpleNamespace(
        status="done",
        origin=origin,
        lang=lang,
        source_fingerprint=compute_source_fingerprint(
            payload.normalizer_version, payload.segments
        ),
        segments=[{"i": s.i, "text_ko": ("한국어 " + s.text) if s.text else ""}
                  for s in payload.segments],
    )
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


class TestComputeSourceFingerprint:
    def test_deterministic(self):
        out = _ok_payload()
        assert compute_source_fingerprint(1, out.segments) == compute_source_fingerprint(
            1, out.segments
        )

    def test_sensitive_to_text_version_and_boundaries(self):
        a = normalize_lyrics(_lyrics_row("ab\nc")).segments
        b = normalize_lyrics(_lyrics_row("a\nbc")).segments  # same chars, shifted boundary
        c = normalize_lyrics(_lyrics_row("ab\nd")).segments
        assert compute_source_fingerprint(1, a) != compute_source_fingerprint(1, b)
        assert compute_source_fingerprint(1, a) != compute_source_fingerprint(1, c)
        assert compute_source_fingerprint(1, a) != compute_source_fingerprint(2, a)

    def test_gap_segments_included(self):
        with_gap = normalize_lyrics(_lyrics_row("a\n\nb")).segments
        without = normalize_lyrics(_lyrics_row("a\nb")).segments
        assert compute_source_fingerprint(1, with_gap) != compute_source_fingerprint(
            1, without
        )


class TestAttachTranslation:
    def test_no_row_reads_as_none(self):
        out = _ok_payload()
        attach_translation(out, None)
        assert out.translation.status == "none"
        assert all(s.text_ko is None for s in out.segments)

    def test_requested_and_failed_pass_through_without_text(self):
        for status in ("requested", "failed"):
            out = _ok_payload()
            row = _done_row(out, origin=None)
            row.status = status
            attach_translation(out, row)
            assert out.translation.status == status
            assert all(s.text_ko is None for s in out.segments)

    def test_done_with_matching_fingerprint_attaches_text_ko(self):
        out = _ok_payload()
        attach_translation(out, _done_row(out))
        assert out.translation.status == "done"
        assert out.translation.origin == "poller"
        assert out.translation.lang == "ko"
        assert out.segments[0].text_ko == "한국어 line one"
        assert out.segments[1].text_ko is None  # stored gap "" stays omitted
        assert out.segments[2].text_ko == "한국어 line two"

    def test_fingerprint_mismatch_reads_as_stale_text_omitted(self):
        out = _ok_payload()
        attach_translation(out, _done_row(out, source_fingerprint="deadbeef"))
        assert out.translation.status == "stale"
        assert all(s.text_ko is None for s in out.segments)

    def test_done_without_stored_segments_reads_as_stale(self):
        out = _ok_payload()
        attach_translation(out, _done_row(out, segments=None))
        assert out.translation.status == "stale"
        assert all(s.text_ko is None for s in out.segments)

    def test_manual_origin_surfaces(self):
        out = _ok_payload()
        attach_translation(out, _done_row(out, origin="manual"))
        assert out.translation.origin == "manual"


class TestGetNormalizedTranslation:
    """get_normalized wiring: the translation view is attached only to ok payloads.

    get_normalized is a single OUTER-JOIN query (cross-region RTT): the mock returns
    one (track_id, lyrics_row, trans_row) row instead of three sequential results."""

    def _db(self, track_id, lyrics_row, trans_row, genius_row=None):
        # Three outerjoins since FEAT-lyrics-annotations: track_lyrics,
        # track_lyrics_translations, track_genius_songs.
        db = MagicMock()
        (
            db.query.return_value
            .outerjoin.return_value
            .outerjoin.return_value
            .outerjoin.return_value
            .filter.return_value
            .one_or_none.return_value
        ) = (track_id, lyrics_row, trans_row, genius_row)
        return db

    def test_ok_payload_carries_translation(self):
        db = self._db(_TRACK_ID, _lyrics_row(), None)
        out = LyricsService().get_normalized(db, spotify_track_id="sid")
        assert out.availability == "ok"
        assert out.translation.status == "none"

    def test_non_ok_payload_skips_translation_attach(self):
        # even with a translation row joined in, a non-ok payload never carries it
        db = self._db(_TRACK_ID, None, SimpleNamespace(status="done"))
        out = LyricsService().get_normalized(db, spotify_track_id="sid")
        assert out.availability == "unavailable"
        assert out.translation is None
        # Single round trip. Asserted on db.query itself rather than by walking the
        # join chain, so adding a join does not break the test that guards the RTT.
        assert db.query.call_count == 1


class TestRequestTranslation:
    def _db(self, track_id, lyrics_row, trans_row):
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = track_id
        db.query.return_value.filter.return_value.one_or_none.side_effect = [
            lyrics_row, trans_row,
        ]
        return db

    def test_unknown_track_raises_not_found(self):
        db = self._db(None, None, None)
        with pytest.raises(LyricsTrackNotFoundError):
            LyricsService().request_translation(db, spotify_track_id="zzz")
        db.commit.assert_not_called()

    def test_no_viewable_lyrics_raises_not_translatable(self):
        # unavailable / no_lyrics / missing row — nothing to translate (route → 409)
        for row in (None, _lyrics_row(match_status="no_lyrics", lyric_plain="")):
            db = self._db(_TRACK_ID, row, None)
            with pytest.raises(LyricsNotTranslatableError):
                LyricsService().request_translation(db, spotify_track_id="sid")
            db.commit.assert_not_called()

    def test_first_request_inserts_requested_row(self):
        db = self._db(_TRACK_ID, _lyrics_row(), None)
        info = LyricsService().request_translation(db, spotify_track_id="sid")
        assert info.status == "requested"
        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        assert added.status == "requested"
        assert added.track_id == _TRACK_ID
        db.commit.assert_called_once()

    def test_pending_request_is_idempotent_noop(self):
        trow = SimpleNamespace(status="requested", claimed_at=None, error=None)
        db = self._db(_TRACK_ID, _lyrics_row(), trow)
        info = LyricsService().request_translation(db, spotify_track_id="sid")
        assert info.status == "requested"
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_rerequest_resets_done_row(self):
        # done/failed/stale rows accept a re-request: claim gate + error reset in place.
        trow = SimpleNamespace(
            status="done", claimed_at="2026-07-04", error="old",
            requested_at="old", updated_at="old",
        )
        db = self._db(_TRACK_ID, _lyrics_row(), trow)
        info = LyricsService().request_translation(db, spotify_track_id="sid")
        assert info.status == "requested"
        assert trow.status == "requested"
        assert trow.claimed_at is None
        assert trow.error is None
        db.add.assert_not_called()
        db.commit.assert_called_once()
