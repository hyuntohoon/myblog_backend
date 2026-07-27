"""genius_anchor + attach_annotations units — FEAT-lyrics-annotations Thread 1.

The load-bearing claims these pin, all from RFC §6.6 and the render spec in
docs/design/lyrics-annotations/README.md:

  * a span is expressed in LyricsSegment.i, NOT list position — a gap row costs an
    index but contributes no text, and getting this wrong shifts every anchor after
    the first stanza break while leaving the match *rate* unchanged (i.e. invisible)
  * a chorus is `repeated`, not a failure
  * annotations survive a track with no lyrics at all
  * a changed Genius body withholds the Korean rather than showing a stale translation
  * ordering is position in the lyrics, never votes
"""
from __future__ import annotations

from types import SimpleNamespace

from app.api.schemas import LyricsResponse, LyricsSegment
from app.services.genius_anchor import anchor_fragments, normalize
from app.services.lyrics_service import attach_annotations, compute_body_fingerprint


def _segs(*texts: str) -> list[LyricsSegment]:
    return [LyricsSegment(i=i, text=t) for i, t in enumerate(texts)]


_LYRIC = _segs(
    "En la vidriera se rompe la luz",
    "y cae en el suelo como agua sin sed",
    "",                                       # stanza gap — holds index 2, no text
    "Quién pudiera vivir sin pedir perdón",
    "",                                       # another gap
    "Quién pudiera vivir sin pedir perdón",   # the chorus, second time
)


# ── anchoring ────────────────────────────────────────────────────────────────

def test_normalize_folds_accents_and_punctuation():
    assert normalize("De Madrugá!") == normalize("De Madruga")
    assert normalize("  ¿Quién,  pudiera? ") == "quien pudiera"


def test_single_line_fragment_resolves_to_one_segment():
    (a,) = anchor_fragments(_LYRIC, ["En la vidriera se rompe la luz"])
    assert (a.status, a.span, a.occurrences) == ("unique", (0, 0), 1)


def test_fragment_spanning_two_segments_resolves_to_the_range():
    # This is the case the module exists for: Genius breaks on the sentence,
    # LRC breaks where the vocal breathes.
    (a,) = anchor_fragments(
        _LYRIC, ["En la vidriera se rompe la luz y cae en el suelo como agua sin sed"]
    )
    assert (a.status, a.span) == ("unique", (0, 1))


def test_span_uses_segment_index_not_list_position():
    """A gap row holds an index but contributes no characters.

    An earlier tool version dropped gaps, which shifted every index after the first
    stanza break — 44.2% of 1,200 prod tracks had at least one row shifted, and the
    match *rate* was unaffected, so the defect was invisible in coverage numbers.
    """
    segs = [LyricsSegment(i=0, text="first"), LyricsSegment(i=1, text=""),
            LyricsSegment(i=2, text="second")]
    (a,) = anchor_fragments(segs, ["second"])
    assert a.span == (2, 2), "must name the row the viewer renders, not the 2nd non-empty one"


def test_chorus_is_repeated_not_a_failure():
    (a,) = anchor_fragments(_LYRIC, ["Quién pudiera vivir sin pedir perdón"])
    assert a.status == "repeated"
    assert a.occurrences == 2
    assert a.span == (3, 3), "the first occurrence is the one that gets rendered"


def test_section_marker_is_classified_not_counted_as_unmatched():
    (a,) = anchor_fragments(_LYRIC, ["[Estribillo]"])
    assert (a.status, a.span) == ("section", None)


def test_absent_fragment_is_unmatched():
    (a,) = anchor_fragments(_LYRIC, ["esta línea no existe en ninguna parte"])
    assert (a.status, a.span) == ("unmatched", None)


def test_head_fallback_places_a_fragment_with_a_trailing_adlib():
    # Genius carries an ad-lib our upload omits; the opening still pins the location.
    (a,) = anchor_fragments(
        _LYRIC, ["En la vidriera se rompe la luz y algo que no cantamos nunca uh uh"]
    )
    assert a.status == "partial"
    assert a.span is not None and a.span[0] == 0


def test_no_segments_degrades_quietly():
    """The no-lyrics case — callers rely on this not raising."""
    out = anchor_fragments([], ["anything at all"])
    assert [a.status for a in out] == ["unmatched"]


# ── attach_annotations ───────────────────────────────────────────────────────

def _row(gid, ordinal, fragment, *, body_ko="한국어 해설", source="english body",
         status="done", votes=5, lang="en", fingerprint=None):
    return SimpleNamespace(
        genius_annotation_id=gid,
        referent_ordinal=ordinal,
        fragment=fragment,
        body_ko=body_ko,
        body_source=source,
        body_source_lang=lang,
        body_source_fingerprint=(
            compute_body_fingerprint(source) if fingerprint is None else fingerprint
        ),
        translation_status=status,
        votes_total=votes,
    )


def _resp(segments):
    return LyricsResponse(availability="ok", source_kind="synced", segments=segments)


_SONG = SimpleNamespace(genius_url="https://genius.com/x")


def test_ordering_is_position_in_the_lyrics_not_ordinal_or_votes():
    out = _resp(_LYRIC)
    rows = [
        _row(1, 1, "Quién pudiera vivir sin pedir perdón", votes=99),  # later in the song
        _row(2, 2, "En la vidriera se rompe la luz", votes=-3),        # earlier in the song
    ]
    attach_annotations(out, _SONG, rows)
    assert [a.id for a in out.annotations] == [2, 1], "earlier span first, votes irrelevant"


def test_unanchored_rows_sort_after_anchored_ones_by_ordinal():
    out = _resp(_LYRIC)
    rows = [
        _row(10, 9, "no aparece en el texto"),
        _row(11, 1, "En la vidriera se rompe la luz"),
        _row(12, 4, "tampoco aparece"),
    ]
    attach_annotations(out, _SONG, rows)
    assert [a.id for a in out.annotations] == [11, 12, 10]


def test_stale_source_withholds_the_korean_body():
    out = _resp(_LYRIC)
    rows = [_row(1, 1, "En la vidriera se rompe la luz", fingerprint="stale-hash")]
    attach_annotations(out, _SONG, rows)
    (a,) = out.annotations
    assert a.translation_status == "stale"
    assert a.body_ko is None, "never show a translation of text that changed"


def test_pending_translation_withholds_the_korean_body():
    out = _resp(_LYRIC)
    rows = [_row(1, 1, "En la vidriera se rompe la luz", status="pending", body_ko="초벌")]
    attach_annotations(out, _SONG, rows)
    assert out.annotations[0].body_ko is None


def test_negative_votes_are_marked_disputed():
    out = _resp(_LYRIC)
    rows = [_row(1, 1, "En la vidriera se rompe la luz", votes=-17)]
    attach_annotations(out, _SONG, rows)
    assert out.annotations[0].disputed is True
    rows = [_row(2, 1, "En la vidriera se rompe la luz", votes=0)]
    out2 = _resp(_LYRIC)
    attach_annotations(out2, _SONG, rows)
    assert out2.annotations[0].disputed is False, "zero is not negative"


def test_annotations_survive_a_track_with_no_lyrics():
    """2 of 15 LUX tracks carry annotations and no synced lyrics."""
    out = LyricsResponse(availability="no_lyrics", segments=[])
    rows = [_row(1, 1, "algo"), _row(2, 2, "otra cosa")]
    attach_annotations(out, _SONG, rows)
    assert len(out.annotations) == 2
    assert all(a.status == "unmatched" for a in out.annotations)
    assert all(a.start_i is None for a in out.annotations)


def test_no_rows_leaves_the_field_empty():
    out = _resp(_LYRIC)
    attach_annotations(out, None, [])
    assert out.annotations == []
