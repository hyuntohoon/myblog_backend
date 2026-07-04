# app/services/lyrics_service.py
"""FEAT-lyrics-viewer Step 1 — read-time lyric normalization behind the JWT-gated read.

Implements the normalized payload spec pinned in ARCH-lyrics-normalization-model
(2026-07-02): one pure, replayable ``normalize_lyrics`` produces the same segment shape
for plain-only / synced-only / mixed rows. Synced-bearing rows derive segments from the
LRC alone (plain is never aligned against synced); a synced-only row is parsed, never
timestamp-stripped to raw text. Raw ``lyric_plain``/``lyric_synced`` stay source-of-truth;
nothing normalized is stored (re-evaluate only on profiling or a second consumer).

Privacy: track_lyrics is owner-only research data with a "never in any shared response"
bar — this module's output leaves the service ONLY via the JWT-gated /api/lyrics read
(its own apigateway.tf route, not the edge_guard catch-all) and must never enter a
public/edge-cached response.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from myblog_shared_db.models import Track, TrackLyrics, TrackLyricsTranslation

from app.api.schemas import LyricsResponse, LyricsSegment, LyricsTranslationInfo

logger = logging.getLogger(__name__)

# Bumped on any segment-shape/derivation change (mirrors matcher_version) so a cached
# normalized payload can be invalidated.
NORMALIZER_VERSION = 1

# Leading LRC timestamp: [mm:ss], [mm:ss.x], [mm:ss.xx], [mm:ss.xxx]. Minutes may
# exceed 59 (two-hour tracks exist), so mm is unbounded digits.
_LRC_TS = re.compile(r"^\[(\d+):(\d{1,2})(?:\.(\d{1,3}))?\]")
# LRC ID tag ([ar:...], [ti:...], ...): non-numeric key → metadata, dropped.
_LRC_ID_TAG = re.compile(r"^\[[A-Za-z#][^\]]*\]\s*$")


class LyricsTrackNotFoundError(Exception):
    """No catalog Track with that spotify_id — the route maps this to 404."""


class LyricsNotTranslatableError(Exception):
    """Track has no viewable lyrics (availability != "ok") — nothing to translate;
    the route maps this to 409."""


def compute_source_fingerprint(normalizer_version: int, segments: List[LyricsSegment]) -> str:
    """sha256 over normalizer_version + every segment text (gaps included, file order).

    The translate poller imports THIS function from the local backend checkout, so
    translate-time and read-time fingerprints agree by construction. Staleness is
    computed at read time by re-deriving this value — never stored (RFC target state).
    """
    h = hashlib.sha256()
    h.update(str(normalizer_version).encode("utf-8"))
    for seg in segments:
        h.update(b"\x00")
        h.update(seg.text.encode("utf-8"))
    return h.hexdigest()


def attach_translation(out: LyricsResponse, trow: Optional[TrackLyricsTranslation]) -> None:
    """Additive translation view on an availability=="ok" payload.

    text_ko is populated only on done + fingerprint match; any mismatch (re-matched
    source, normalizer bump, missing stored segments) reads as "stale" with text_ko
    omitted — the viewer then offers re-request instead of showing a misaligned
    translation. Stored gap entries ("") stay omitted (text_ko=None).
    """
    if trow is None:
        out.translation = LyricsTranslationInfo(status="none")
        return
    status = trow.status
    if status == "done":
        stored = trow.segments or []
        current = compute_source_fingerprint(out.normalizer_version, out.segments)
        if trow.source_fingerprint != current or not stored:
            status = "stale"
        else:
            by_i = {s.get("i"): s.get("text_ko") for s in stored if isinstance(s, dict)}
            for seg in out.segments:
                ko = by_i.get(seg.i)
                if ko:
                    seg.text_ko = ko
    out.translation = LyricsTranslationInfo(status=status, origin=trow.origin, lang=trow.lang)


def _ts_to_ms(mm: str, ss: str, frac: Optional[str]) -> int:
    ms = int(mm) * 60_000 + int(ss) * 1_000
    if frac:
        ms += int(frac) * (100, 10, 1)[len(frac) - 1]
    return ms


def parse_lrc(lyric_synced: str) -> List[LyricsSegment]:
    """Parse LRC text into ordered segments (pinned rules 1-6).

    One segment per leading timestamp (multi-timestamp lines don't occur in the corpus;
    if a source introduces them, each timestamp becomes its own segment — additive).
    Blank-timestamp lines are kept as gap segments (text ``""``); ID-tag lines are
    dropped; any other non-empty untimestamped line is kept with ``start_ms: null`` and
    flagged (text is never silently dropped). File order is preserved — an out-of-order
    timestamp is a diagnostics flag, not a re-sort. Indexes are assigned by the caller.
    """
    segments: List[LyricsSegment] = []
    flagged_untimestamped = 0
    last_ms: Optional[int] = None
    out_of_order = False

    for raw_line in lyric_synced.split("\n"):
        line = raw_line.rstrip("\r")
        starts: List[int] = []
        m = _LRC_TS.match(line)
        while m:
            starts.append(_ts_to_ms(m.group(1), m.group(2), m.group(3)))
            line = line[m.end():]
            m = _LRC_TS.match(line)

        if not starts:
            if not line.strip():
                continue  # untimestamped blank — nothing to keep
            if _LRC_ID_TAG.match(line):
                continue  # metadata tag, dropped
            segments.append(LyricsSegment(i=0, text=line, start_ms=None))
            flagged_untimestamped += 1
            continue

        text = line[1:] if line.startswith(" ") else line  # one leading space trimmed
        if not text.strip():
            text = ""  # gap segment (stanza marker); text=="" is the discriminator
        for start_ms in starts:
            if last_ms is not None and start_ms < last_ms:
                out_of_order = True
            last_ms = start_ms
            segments.append(LyricsSegment(i=0, text=text, start_ms=start_ms))

    if flagged_untimestamped:
        logger.warning(
            "lyrics parse_lrc: %d untimestamped non-tag line(s) kept with start_ms=null",
            flagged_untimestamped,
        )
    if out_of_order:
        logger.warning("lyrics parse_lrc: out-of-order timestamp(s) — file order preserved")
    return segments


def _plain_disagrees(lyric_plain: str, segments: List[LyricsSegment]) -> bool:
    """Diagnostics only, never merges: compare non-blank line counts + whitespace-normalized
    text of plain vs the parsed segments' non-gap text. Synced always wins."""
    plain_lines = [ln.strip() for ln in lyric_plain.split("\n") if ln.strip()]
    synced_lines = [s.text.strip() for s in segments if s.text.strip()]
    if len(plain_lines) != len(synced_lines):
        return True
    norm = lambda lines: re.sub(r"\s+", " ", " ".join(lines))  # noqa: E731
    return norm(plain_lines) != norm(synced_lines)


def normalize_lyrics(row: Optional[TrackLyrics]) -> LyricsResponse:
    """Pure, replayable normalizer (pinned pseudocode): same row in ⇒ identical payload out.

    matched+synced (mixed 91% AND defensive synced-only) → parse LRC, one code path;
    matched plain-only (9%) → one segment per line, blank lines kept as gaps;
    no_lyrics → instrumental empty state; unresolved/missing/defensive-empty → unavailable.
    """
    if row is None or row.match_status in ("not_found", "ambiguous", "review_required"):
        return LyricsResponse(availability="unavailable")
    if row.match_status == "no_lyrics":
        return LyricsResponse(availability="no_lyrics")

    # match_status == "matched" (V33: lyric_plain is non-NULL here, possibly "")
    if (row.lyric_synced or "").strip():
        segments = parse_lrc(row.lyric_synced)
        source_kind = "synced"
        if (row.lyric_plain or "").strip() and _plain_disagrees(row.lyric_plain, segments):
            # ~2% residual (probe 2026-07-02); synced text+timestamp are self-consistent.
            logger.warning(
                "lyrics normalize: plain != stripped synced (synced wins) track_id=%s",
                row.track_id,
            )
    elif (row.lyric_plain or "").strip():
        # Whitespace-only plain lines normalize to "" so the gap discriminator
        # (text == "") fires for them too — the viewer skips gaps in focus nav.
        segments = [
            LyricsSegment(i=0, text=t.rstrip("\r") if t.strip() else "", start_ms=None)
            for t in row.lyric_plain.split("\n")
        ]
        source_kind = "plain"
    else:
        return LyricsResponse(availability="unavailable")  # defensive: matched but empty

    for i, seg in enumerate(segments):
        seg.i = i
    trackable = any(s.start_ms is not None and s.text != "" for s in segments)
    return LyricsResponse(
        availability="ok",
        source_kind=source_kind,
        trackable=trackable,
        normalizer_version=NORMALIZER_VERSION,
        segments=segments,
    )


class LyricsService:
    def get_normalized(self, db: Session, *, spotify_track_id: str) -> LyricsResponse:
        """Resolve the playback-side spotify_track_id → catalog Track server-side (reverse
        of PlaybackService.resolve_uri, one round trip), then normalize its lyrics row.
        Unknown spotify_id → LyricsTrackNotFoundError (404); known track without a
        track_lyrics row → availability "unavailable" (a valid empty state, not a 404).
        availability=="ok" payloads additionally carry the translation view (Step 2)."""
        track_id = db.query(Track.id).filter(Track.spotify_id == spotify_track_id).scalar()
        if track_id is None:
            raise LyricsTrackNotFoundError(spotify_track_id)
        row = (
            db.query(TrackLyrics).filter(TrackLyrics.track_id == track_id).one_or_none()
        )
        out = normalize_lyrics(row)
        if out.availability != "ok":
            return out
        trow = (
            db.query(TrackLyricsTranslation)
            .filter(TrackLyricsTranslation.track_id == track_id)
            .one_or_none()
        )
        attach_translation(out, trow)
        return out

    def request_translation(self, db: Session, *, spotify_track_id: str) -> LyricsTranslationInfo:
        """Upsert status='requested' for one designated track (FEAT-lyrics-translation).

        Idempotent while pending — a second POST on a 'requested' row is a no-op.
        Allowed again on done/failed/stale (an explicit re-request may overwrite a
        'manual' row; only the poller is barred from touching manual otherwise).
        A track without viewable lyrics is rejected (LyricsNotTranslatableError → 409):
        there is nothing for the poller to normalize or translate."""
        track_id = db.query(Track.id).filter(Track.spotify_id == spotify_track_id).scalar()
        if track_id is None:
            raise LyricsTrackNotFoundError(spotify_track_id)
        lrow = (
            db.query(TrackLyrics).filter(TrackLyrics.track_id == track_id).one_or_none()
        )
        if normalize_lyrics(lrow).availability != "ok":
            raise LyricsNotTranslatableError(spotify_track_id)
        trow = (
            db.query(TrackLyricsTranslation)
            .filter(TrackLyricsTranslation.track_id == track_id)
            .one_or_none()
        )
        if trow is not None and trow.status == "requested":
            return LyricsTranslationInfo(status="requested")
        if trow is None:
            db.add(TrackLyricsTranslation(track_id=track_id, status="requested"))
        else:
            # Re-request: reset the claim gate and prior error; the poller overwrites
            # segments/fingerprint on completion, so stale result data may stay behind
            # the non-'done' status until then.
            trow.status = "requested"
            trow.claimed_at = None
            trow.error = None
            trow.requested_at = func.now()
            trow.updated_at = func.now()
        db.commit()
        return LyricsTranslationInfo(status="requested")
