# app/services/lyrics_report_service.py
"""FEAT-lyrics-match-correction Step 3b — member lyrics reports ("가사가 틀려요").

A report never changes ``track_lyrics``. It records what the member saw (a snapshot of the
served row), enqueues the existing single-track re-check when it is the track's first open
report, and puts the track on the owner's queue, which opens the 3a panel. The owner's panel
decisions close the track's open reports (``resolve_open_reports`` in
``lyrics_admin_service``).

* **Reportable:** the viewer's own verdicts — ``availability`` ``ok`` (lyrics shown) or
  ``no_lyrics``. Anything else is not a lyric anyone saw.
* **Limits:** one open report per (track, member), enforced by the V64 partial unique; at
  most ``LYRICS_REPORT_DAILY_CAP`` reports per member per rolling 24 h, resolved included.
* **Re-check:** only on the track's first open report, with a cooldown, so many members
  reporting one track cost one LRCLIB search at most. Advisory locks, per member then per
  track, serialise a member's concurrent reports (the cap) and concurrent first reporters
  (one re-check); they are held only by the short insert transaction.

Every DB write commits before any SQS send: no transaction is held across an external call.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from myblog_shared_db.models import (
    Artist,
    Track,
    TrackLyrics,
    TrackLyricsReport,
    track_artists_table,
)

from app.api.schemas import AdminLyricsReport, AdminLyricsReports, AdminLyricsReported
from app.services.lyrics_admin_service import _current_lrclib_id, current_view
from app.services.lyrics_service import normalize_lyrics

logger = logging.getLogger(__name__)

REPORTABLE = ("ok", "no_lyrics")
MEMBER_REASON = "member"
# A member's re-check is a no-op at the worker when the row was written or re-judged in
# the last hour; the owner's refresh (cooldown 0) is the way to force one.
MEMBER_RECHECK_COOLDOWN_SEC = 3600
QUEUE_TRACK_LIMIT = 200


class LyricsReportTrackNotFound(Exception):
    """No catalog track with that spotify id → 404."""


class LyricsReportNotReportable(Exception):
    """The track shows neither lyrics nor a "no lyrics" verdict → 409."""


class LyricsReportRateLimited(Exception):
    """The member's rolling-24h report cap is spent → 429."""


@dataclass(frozen=True)
class ReportOutcome:
    outcome: str  # reported | already_reported
    recheck_enqueued: bool = False  # internal: never returned to the member


def body_sha(row: Optional[TrackLyrics]) -> Optional[str]:
    """What the viewer renders, hashed: both bodies of a ``matched`` row. None otherwise.
    Comparing it with a later read tells "same lyric, new LRCLIB id" from a real change."""
    if row is None or row.match_status != "matched":
        return None
    raw = f"{row.lyric_synced or ''}\x00{row.lyric_plain or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _advisory_key(kind: str, key: uuid.UUID) -> int:
    # Signed 64-bit key for pg_advisory_xact_lock, namespaced so it collides with no other
    # advisory user by accident. Step 4's system-report writer must take the same
    # ("track", track_id) key, or two first reports could both enqueue a re-check.
    digest = hashlib.sha256(f"lyrics_report:{kind}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class LyricsReportService:
    def __init__(self, sqs=None) -> None:
        # Lazy so read paths and tests never build a boto3 client.
        self._sqs = sqs

    @property
    def sqs(self):
        if self._sqs is None:
            from app.clients.sqs_client import SqsClient

            self._sqs = SqsClient()
        return self._sqs

    # ── member ──────────────────────────────────────────────────────────────────────

    def report(
        self, db: Session, *, spotify_track_id: str, member_id: uuid.UUID, daily_cap: int,
    ) -> ReportOutcome:
        track_id = db.query(Track.id).filter(Track.spotify_id == spotify_track_id).scalar()
        if track_id is None:
            raise LyricsReportTrackNotFound(spotify_track_id)
        # Serialise until commit, member first, then track (one fixed order, so no
        # deadlock): the member's 24h count below must not be read by two of their
        # reports on different tracks at once, and the "first open report" answer must
        # not be read by two first reporters at once.
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key("member", member_id)})
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key("track", track_id)})
        row = db.query(TrackLyrics).filter(TrackLyrics.track_id == track_id).one_or_none()
        if normalize_lyrics(row).availability not in REPORTABLE:
            db.rollback()
            raise LyricsReportNotReportable(spotify_track_id)

        open_reports = (
            select(TrackLyricsReport.id)
            .where(TrackLyricsReport.track_id == track_id, TrackLyricsReport.status == "open")
        )
        mine = db.scalar(open_reports.where(TrackLyricsReport.user_id == member_id).limit(1))
        if mine is not None:
            db.rollback()
            return ReportOutcome("already_reported")

        recent = db.scalar(
            select(func.count())
            .select_from(TrackLyricsReport)
            .where(
                TrackLyricsReport.user_id == member_id,
                TrackLyricsReport.created_at > func.now() - text("interval '24 hours'"),
            )
        )
        if recent is not None and recent >= daily_cap:
            db.rollback()
            raise LyricsReportRateLimited(f"{recent}/{daily_cap} in 24h")

        first = db.scalar(open_reports.limit(1)) is None
        # RETURNING, not a pre-check alone: the partial unique is the real guard (a double
        # tap from another session the advisory lock already serialised still lands here).
        inserted = db.execute(
            pg_insert(TrackLyricsReport)
            .values(
                track_id=track_id,
                user_id=member_id,
                reason=MEMBER_REASON,
                reported_status=row.match_status if row is not None else None,
                reported_lrclib_id=_current_lrclib_id(row),
                reported_body_sha=body_sha(row),
            )
            .on_conflict_do_nothing(
                index_elements=["track_id", "user_id"],
                index_where=text("status = 'open' AND user_id IS NOT NULL"),
            )
            .returning(TrackLyricsReport.id)
        ).first()
        db.commit()
        if inserted is None:
            return ReportOutcome("already_reported")
        logger.info("Lyrics report: track %s reported (first open=%s)", track_id, first)
        if not first:
            return ReportOutcome("reported")
        try:
            sent = self.sqs.send_lyrics_recheck(str(track_id), cooldown_sec=MEMBER_RECHECK_COOLDOWN_SEC)
        except Exception as error:  # noqa: BLE001 - the report stands; the owner queue holds it
            logger.error("Lyrics report: re-check not enqueued for %s (%s)", track_id, type(error).__name__)
            sent = False
        return ReportOutcome("reported", recheck_enqueued=sent)

    # ── owner ───────────────────────────────────────────────────────────────────────

    def queue(self, db: Session) -> AdminLyricsReports:
        """Open reports grouped by track, most reported first. ``auto_resolved`` is derived
        here and never stored: the served status or body differs from what the latest
        report saw, so a re-check (or the owner) has changed it since."""
        open_tracks = (
            select(
                TrackLyricsReport.track_id,
                func.count().label("n"),
                func.max(TrackLyricsReport.created_at).label("last_at"),
            )
            .where(TrackLyricsReport.status == "open")
            .group_by(TrackLyricsReport.track_id)
            .subquery()
        )
        total = db.scalar(select(func.count()).select_from(open_tracks)) or 0
        top = db.execute(
            select(open_tracks.c.track_id)
            .order_by(open_tracks.c.n.desc(), open_tracks.c.last_at.desc(), open_tracks.c.track_id)
            .limit(QUEUE_TRACK_LIMIT)
        ).scalars().all()
        if not top:
            db.rollback()
            return AdminLyricsReports(items=[], total_tracks=0, truncated=False)

        reports = (
            db.query(TrackLyricsReport)
            .filter(TrackLyricsReport.track_id.in_(top), TrackLyricsReport.status == "open")
            .order_by(TrackLyricsReport.created_at)
            .all()
        )
        tracks = {t.id: t for t in db.query(Track).filter(Track.id.in_(top)).all()}
        rows = {r.track_id: r for r in db.query(TrackLyrics).filter(TrackLyrics.track_id.in_(top)).all()}
        artists: Dict[Any, List[str]] = {}
        for tid, name in (
            db.query(track_artists_table.c.track_id, Artist.name)
            .join(Artist, Artist.id == track_artists_table.c.artist_id)
            .filter(track_artists_table.c.track_id.in_(top))
            .order_by(Artist.popularity.desc().nullslast(), Artist.name)
            .all()
        ):
            artists.setdefault(tid, []).append(name)

        # Everything below reads the loaded objects, so it runs BEFORE the rollback that
        # ends this read: a rollback expires them, and each attribute read after it would
        # be its own SELECT in a new transaction.
        by_track: Dict[Any, List[TrackLyricsReport]] = {}
        for r in reports:
            by_track.setdefault(r.track_id, []).append(r)

        items: List[AdminLyricsReport] = []
        for tid in top:
            group = by_track.get(tid)
            track = tracks.get(tid)
            if not group or track is None:
                continue  # resolved or deleted between the two reads
            latest = group[-1]
            row = rows.get(tid)
            changed = (
                (row.match_status if row is not None else None) != latest.reported_status
                or body_sha(row) != latest.reported_body_sha
            )
            reasons = Counter(r.reason for r in group)
            items.append(AdminLyricsReport(
                spotify_track_id=track.spotify_id,
                track_title=track.title,
                track_artists=artists.get(tid, []),
                report_count=len(group),
                member_count=reasons.get(MEMBER_REASON, 0),
                system_reasons=sorted(k for k in reasons if k != MEMBER_REASON),
                first_reported_at=group[0].created_at,
                last_reported_at=latest.created_at,
                reported=AdminLyricsReported(
                    match_status=latest.reported_status, lrclib_id=latest.reported_lrclib_id,
                ),
                current=current_view(row),
                state="auto_resolved" if changed else "open",
            ))
        db.rollback()  # read only
        return AdminLyricsReports(items=items, total_tracks=total, truncated=total > len(top))

