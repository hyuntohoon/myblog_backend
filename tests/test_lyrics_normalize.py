"""normalize_lyrics / parse_lrc units — the pinned ARCH-lyrics-normalization-model spec.

Fixtures mirror the real corpus shapes (LRCLIB): mixed rows carry synced LRC with blank
stanza-marker lines (93% of rows), plain-only rows carry \n-separated text, and the V33
CHECK means matched/no_lyrics rows always have non-NULL lyric_plain (possibly "").
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.lyrics_service import normalize_lyrics, parse_lrc

_LRC = (
    "[ar:Artist]\n"
    "[00:12.34] first line\n"
    "[00:15.1]second line\r\n"
    "[00:20]\n"
    "[01:02.345]third line\n"
)


def _row(match_status="matched", lyric_plain=None, lyric_synced=None):
    return SimpleNamespace(
        track_id="11111111-1111-1111-1111-111111111111",
        match_status=match_status,
        lyric_plain=lyric_plain,
        lyric_synced=lyric_synced,
    )


class TestParseLrc:
    def test_timestamp_grammar_and_frac_scaling(self):
        segs = parse_lrc("[00:12.34]a\n[00:15.1]b\n[01:02.345]c\n[02:03]d")
        assert [s.start_ms for s in segs] == [12_340, 15_100, 62_345, 123_000]

    def test_minutes_may_exceed_59(self):
        segs = parse_lrc("[75:00.00]deep cut")
        assert segs[0].start_ms == 75 * 60_000

    def test_one_leading_space_trimmed_and_cr_stripped(self):
        segs = parse_lrc("[00:01.00] one leading space kept?\r")
        assert segs[0].text == "one leading space kept?"
        segs = parse_lrc("[00:01.00]  double space keeps one")
        assert segs[0].text == " double space keeps one"

    def test_blank_timestamp_line_is_gap_segment(self):
        segs = parse_lrc("[00:01.00]a\n[00:02.00]\n[00:03.00]b")
        assert [s.text for s in segs] == ["a", "", "b"]
        assert segs[1].start_ms == 2_000  # gap keeps its timestamp

    def test_id_tags_dropped_untimestamped_text_kept(self):
        segs = parse_lrc("[ar:Artist]\n[ti:Title]\nstray text\n[00:01.00]a")
        assert [s.text for s in segs] == ["stray text", "a"]
        assert segs[0].start_ms is None  # kept, flagged, never silently dropped

    def test_file_order_preserved_never_resorted(self):
        segs = parse_lrc("[00:10.00]late\n[00:05.00]early")
        assert [s.text for s in segs] == ["late", "early"]

    def test_multi_timestamp_line_one_segment_per_timestamp(self):
        # Absent from the corpus (0/2000) but additive-ready per the pinned spec.
        segs = parse_lrc("[00:01.00][00:05.00]chorus")
        assert [(s.text, s.start_ms) for s in segs] == [("chorus", 1_000), ("chorus", 5_000)]


class TestNormalizeLyrics:
    def test_mixed_derives_from_synced_alone(self):
        # Dominant 91% case: plain present but segments come from the LRC (same path as
        # synced-only) — texts match the LRC lines, timestamps kept, ID tag dropped.
        out = normalize_lyrics(_row(lyric_plain="first line\nsecond line\n\nthird line",
                                    lyric_synced=_LRC))
        assert out.availability == "ok"
        assert out.source_kind == "synced"
        assert out.trackable is True
        assert [s.text for s in out.segments] == ["first line", "second line", "", "third line"]
        assert out.segments[0].start_ms == 12_340

    def test_synced_only_is_parsed_never_raw_text(self):
        # V33 stored state lyric_plain='' + synced present ⇒ path 2 (the hard non-goal:
        # never a timestamp strip / raw-text fallback).
        out = normalize_lyrics(_row(lyric_plain="", lyric_synced=_LRC))
        assert out.availability == "ok"
        assert out.source_kind == "synced"
        assert all(s.start_ms is not None for s in out.segments if s.text)

    def test_plain_only_null_timestamps_not_trackable(self):
        # The whitespace-only line normalizes to "" so it reads as a gap too.
        out = normalize_lyrics(_row(lyric_plain="line one\n  \nline two"))
        assert out.availability == "ok"
        assert out.source_kind == "plain"
        assert out.trackable is False
        assert [(s.text, s.start_ms) for s in out.segments] == [
            ("line one", None), ("", None), ("line two", None)]

    def test_indexes_are_contiguous_zero_based(self):
        out = normalize_lyrics(_row(lyric_plain="a\nb\nc"))
        assert [s.i for s in out.segments] == [0, 1, 2]

    def test_no_lyrics_and_unresolved_map_to_empty_states(self):
        assert normalize_lyrics(_row("no_lyrics", lyric_plain="")).availability == "no_lyrics"
        for status in ("not_found", "ambiguous", "review_required"):
            out = normalize_lyrics(_row(status))
            assert out.availability == "unavailable"
            assert out.segments == []
        assert normalize_lyrics(None).availability == "unavailable"

    def test_matched_but_empty_is_defensive_unavailable(self):
        out = normalize_lyrics(_row(lyric_plain="", lyric_synced=""))
        assert out.availability == "unavailable"

    def test_gap_only_synced_is_not_trackable(self):
        # trackable requires a NON-GAP timestamped segment.
        out = normalize_lyrics(_row(lyric_plain="", lyric_synced="[00:01.00]\n[00:02.00]"))
        assert out.availability == "ok"
        assert out.trackable is False

    def test_pure_and_replayable(self):
        row = _row(lyric_plain="first line", lyric_synced=_LRC)
        assert normalize_lyrics(row) == normalize_lyrics(row)

    def test_acceptance_gate_identical_shape_across_paths(self):
        # ARCH-lyrics-normalization-model acceptance gate: plain-only / synced-only /
        # mixed all return the identical payload shape.
        outs = [
            normalize_lyrics(_row(lyric_plain="a\nb")),
            normalize_lyrics(_row(lyric_plain="", lyric_synced="[00:01.00]a")),
            normalize_lyrics(_row(lyric_plain="a", lyric_synced="[00:01.00]a")),
        ]
        shapes = [set(o.model_dump().keys()) for o in outs]
        assert shapes[0] == shapes[1] == shapes[2] == {
            "availability", "source_kind", "trackable", "normalizer_version", "segments",
            "translation"}
        for o in outs:
            assert o.availability == "ok"
            assert {"i", "text", "start_ms", "text_ko"} == set(o.segments[0].model_dump().keys())

    def test_plain_disagreement_logged_synced_still_wins(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="app.services.lyrics_service"):
            out = normalize_lyrics(_row(lyric_plain="totally different text",
                                        lyric_synced="[00:01.00]actual lyric"))
        assert out.source_kind == "synced"
        assert out.segments[0].text == "actual lyric"
        assert any("synced wins" in r.message for r in caplog.records)
