# app/services/lyrics_admin_service.py
"""FEAT-lyrics-match-correction Step 3a — the owner's lyrics candidate panel.

Reads the candidate set the worker stored for a track (``track_lyrics_candidates``) next to
what the track serves now, and applies the owner's decisions:

* **no_lyrics** — DB only: the row becomes ``no_lyrics`` with basis ``manual`` (locked).
* **reject X** — DB only: records the rejection. When X is the lyric the track serves, the
  row is taken down at once (``review_required``, reason ``owner_rejected``, unlocked) and a
  re-check is enqueued; the unresolved queue would pick it up anyway if that send failed.
* **unlock** — DB only: drops the lock and enqueues a re-check, so the row is automatic again.
* **pick X** — validated here, written by the worker (``lyrics_pick``), which re-fetches the
  body from LRCLIB by id. A user-facing request never calls LRCLIB.
* **refresh** — enqueues the worker's single-track re-check, which stores a new set.
* **keep** (3b) — DB only: "what it serves is right". Closes the track's open reports and
  changes nothing else; no lock.

Decisions that settle what the track serves close its open reports (3b): keep, no_lyrics and
a reject of the served lyric in their own transaction; pick after its send succeeds. Unlock
and a reject of any other candidate leave them open.

Every DB write commits before any SQS send: no transaction is held across an external call.
The lock keys (``match_basis = 'manual'`` / ``manual_fix``) mirror the worker's
``locked_row_sql`` — the worker's writer is what enforces them for every producer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from myblog_shared_db.models import (
    Artist,
    Track,
    TrackLyrics,
    TrackLyricsCandidate,
    TrackLyricsRejection,
    TrackLyricsReport,
    track_artists_table,
)

from app.api.schemas import (
    AdminLyricsCandidate,
    AdminLyricsCandidates,
    AdminLyricsCurrent,
    AdminLyricsDecisionResult,
)

logger = logging.getLogger(__name__)

BASIS_MANUAL = "manual"
BASIS_UNLOCKED = "unlocked"  # rung 0 on the worker's ladder: any automatic match replaces it
MANUAL_MATCHER_VERSION = "manual"
STALE_AFTER = timedelta(days=7)

# The panel's order: what would win first, then what lost and why, the owner's rejections last.
_VERDICT_ORDER = {
    v: i for i, v in enumerate([
        "picked", "same_body", "outvoted", "version_mismatch", "fuzzy_title", "undecided",
        "no_body", "duration_mismatch", "title_mismatch", "artist_mismatch", "rejected",
    ])
}


class LyricsAdminTrackNotFound(Exception):
    """No catalog track with that spotify id → 404."""


class LyricsAdminConflict(Exception):
    """The decision does not apply to the stored state → 409. ``code`` says why."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LyricsAdminQueueUnavailable(Exception):
    """A pick needs the worker and no queue is configured (local/dev) → 503."""


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _current_lrclib_id(row: Optional[TrackLyrics]) -> Optional[int]:
    if row is None:
        return None
    ev = row.evidence or {}
    picked = ev.get("lrclib_id")
    if picked is None and isinstance(ev.get("promotion"), dict):
        picked = ev["promotion"].get("lrclib_id")
    return _int(picked)


def _lock_of(row: Optional[TrackLyrics]) -> Optional[str]:
    if row is None:
        return None
    ev = row.evidence or {}
    if ev.get("match_basis") == BASIS_MANUAL:
        return "manual"
    if "manual_fix" in ev:
        return "manual_fix"
    return None


def current_view(row: Optional[TrackLyrics]) -> AdminLyricsCurrent:
    if row is None:
        return AdminLyricsCurrent()
    ev = row.evidence or {}
    return AdminLyricsCurrent(
        match_status=row.match_status,
        match_basis=ev.get("match_basis"),
        lrclib_id=_current_lrclib_id(row),
        matcher_version=row.matcher_version,
        lock=_lock_of(row),
        reason=ev.get("reason") if isinstance(ev.get("reason"), str) else None,
        updated_at=row.updated_at,
    )


def _previous(row: Optional[TrackLyrics]) -> Dict[str, Any]:
    if row is None:
        return {"status": None}
    return {
        "status": row.match_status,
        "basis": (row.evidence or {}).get("match_basis"),
        "lrclib_id": _current_lrclib_id(row),
        "matcher_version": row.matcher_version,
        "lock": _lock_of(row),
    }


def _stamp(action: str, actor: str, row: Optional[TrackLyrics], **extra: Any) -> Dict[str, Any]:
    return {
        "action": action,
        "by": actor,
        "at": datetime.now(timezone.utc).isoformat(),
        "previous": _previous(row),
        **extra,
    }


def resolve_open_reports(db: Session, track_id, resolution: str, actor: str) -> int:
    """Close the track's open reports with the owner's action, inside the caller's
    transaction (the caller commits). Returns how many it closed."""
    result = db.execute(
        update(TrackLyricsReport)
        .where(TrackLyricsReport.track_id == track_id, TrackLyricsReport.status == "open")
        .values(
            status="resolved", resolution=resolution, resolved_by=actor,
            resolved_at=datetime.now(timezone.utc),
        )
    )
    return int(result.rowcount or 0)


class LyricsAdminService:
    def __init__(self, sqs=None) -> None:
        # Lazy so read paths and tests never build a boto3 client.
        self._sqs = sqs

    @property
    def sqs(self):
        if self._sqs is None:
            from app.clients.sqs_client import SqsClient

            self._sqs = SqsClient()
        return self._sqs

    # ── read ────────────────────────────────────────────────────────────────────────

    def _track(self, db: Session, spotify_track_id: str) -> Track:
        track = db.query(Track).filter(Track.spotify_id == spotify_track_id).one_or_none()
        if track is None:
            raise LyricsAdminTrackNotFound(spotify_track_id)
        return track

    def candidates(self, db: Session, *, spotify_track_id: str) -> AdminLyricsCandidates:
        track = self._track(db, spotify_track_id)
        artists = [
            name for (name,) in (
                db.query(Artist.name)
                .join(track_artists_table, track_artists_table.c.artist_id == Artist.id)
                .filter(track_artists_table.c.track_id == track.id)
                .order_by(Artist.popularity.desc().nullslast(), Artist.name)
                .all()
            )
        ]
        row = db.query(TrackLyrics).filter(TrackLyrics.track_id == track.id).one_or_none()
        stored = (
            db.query(TrackLyricsCandidate).filter(TrackLyricsCandidate.track_id == track.id).all()
        )
        rejected = sorted(
            lid for (lid,) in db.query(TrackLyricsRejection.lrclib_id)
            .filter(TrackLyricsRejection.track_id == track.id).all()
        )
        current = current_view(row)
        rejected_set = set(rejected)
        items = sorted(
            (
                AdminLyricsCandidate(
                    lrclib_id=c.lrclib_id, title=c.title, artist=c.artist, album=c.album,
                    duration_sec=c.duration_sec, has_synced=c.has_synced, has_plain=c.has_plain,
                    instrumental=c.instrumental, preview=c.preview, cluster_size=c.cluster_size,
                    # A rejection made after the set was stored still reads as rejected.
                    verdict="rejected" if c.lrclib_id in rejected_set else c.verdict,
                    rejected=c.lrclib_id in rejected_set,
                    current=current.lrclib_id is not None and c.lrclib_id == current.lrclib_id,
                )
                for c in stored
            ),
            key=lambda c: (_VERDICT_ORDER.get(c.verdict, len(_VERDICT_ORDER) - 1),
                           -(c.cluster_size or 0), c.lrclib_id),
        )
        fetched_at = max((c.fetched_at for c in stored), default=None)
        stale = (
            fetched_at is None
            or fetched_at < datetime.now(timezone.utc) - STALE_AFTER
            or (current.lrclib_id is not None
                and current.match_status in ("matched", "no_lyrics")
                and all(c.lrclib_id != current.lrclib_id for c in stored))
        )
        return AdminLyricsCandidates(
            spotify_track_id=spotify_track_id,
            track_title=track.title,
            track_artists=artists,
            duration_sec=track.duration_sec,
            current=current,
            candidates=items,
            fetched_at=fetched_at,
            matcher_version=stored[0].matcher_version if stored else None,
            stale=stale,
            rejected_lrclib_ids=rejected,
        )

    # ── refresh ─────────────────────────────────────────────────────────────────────

    def refresh(self, db: Session, *, spotify_track_id: str) -> bool:
        track = self._track(db, spotify_track_id)
        track_id = str(track.id)
        db.rollback()  # end the read before the send
        try:
            return self.sqs.send_lyrics_recheck(track_id)
        except Exception as error:  # noqa: BLE001 - any send failure is "queue unavailable"
            logger.error("Lyrics panel: refresh not enqueued for %s (%s)", track_id, type(error).__name__)
            raise LyricsAdminQueueUnavailable() from error

    # ── decisions ───────────────────────────────────────────────────────────────────

    def decide(
        self, db: Session, *, spotify_track_id: str, action: str,
        lrclib_id: Optional[int], actor: str,
    ) -> AdminLyricsDecisionResult:
        track = self._track(db, spotify_track_id)
        track_id = track.id
        # Row lock: the worker may be writing this row; the decision is a short transaction.
        row = (
            db.query(TrackLyrics).filter(TrackLyrics.track_id == track_id)
            .with_for_update().one_or_none()
        )
        if action == "pick":
            return self._pick(db, track_id, row, lrclib_id, actor)
        if action == "no_lyrics":
            return self._no_lyrics(db, track_id, row, actor)
        if action == "reject":
            return self._reject(db, track_id, row, lrclib_id, actor)
        if action == "keep":
            return self._keep(db, track_id, row, actor)
        if action == "unlock":
            return self._unlock(db, track_id, row, actor)
        db.rollback()
        raise ValueError(f"unknown lyrics decision {action!r}")  # the schema Literal keeps it out

    def _pick(self, db, track_id, row, lrclib_id, actor) -> AdminLyricsDecisionResult:
        in_set = db.query(TrackLyricsCandidate.lrclib_id).filter(
            TrackLyricsCandidate.track_id == track_id,
            TrackLyricsCandidate.lrclib_id == lrclib_id,
        ).first()
        rejected = db.query(TrackLyricsRejection.lrclib_id).filter(
            TrackLyricsRejection.track_id == track_id,
            TrackLyricsRejection.lrclib_id == lrclib_id,
        ).first()
        current = current_view(row)
        db.rollback()  # nothing to write here; release the row lock before the send
        if rejected is not None:
            raise LyricsAdminConflict("rejected")
        if in_set is None:
            raise LyricsAdminConflict("not_a_candidate")
        try:
            sent = self.sqs.send_lyrics_pick(str(track_id), lrclib_id, actor)
        except Exception as error:  # noqa: BLE001 - any send failure is "queue unavailable"
            logger.error("Lyrics panel: pick not enqueued for %s (%s)", track_id, type(error).__name__)
            raise LyricsAdminQueueUnavailable() from error
        if not sent:
            raise LyricsAdminQueueUnavailable()
        # The pick is in flight: close the reports now, in a fresh short transaction. If the
        # worker then refuses it, the row is unchanged and members can report it again.
        resolved = self._resolve_after_send(db, track_id, "pick", actor)
        return AdminLyricsDecisionResult(
            action="pick", outcome="enqueued", reports_resolved=resolved, current=current,
        )

    def _no_lyrics(self, db, track_id, row, actor) -> AdminLyricsDecisionResult:
        evidence = {
            "match_basis": BASIS_MANUAL,
            "manual": _stamp("no_lyrics", actor, row),
        }
        if row is None:
            row = TrackLyrics(track_id=track_id)
            db.add(row)
        row.match_status = "no_lyrics"
        row.lyric_plain = ""  # V33: a resolved status needs a non-NULL plain
        row.lyric_synced = None
        row.evidence = evidence
        row.matcher_version = MANUAL_MATCHER_VERSION
        row.updated_at = datetime.now(timezone.utc)
        resolved = resolve_open_reports(db, track_id, "no_lyrics", actor)
        db.commit()
        current = self._settled_view(db, row)
        logger.warning("Lyrics panel: no_lyrics locked for track %s by %s", track_id, actor)
        return AdminLyricsDecisionResult(
            action="no_lyrics", outcome="applied", reports_resolved=resolved, current=current,
        )

    def _reject(self, db, track_id, row, lrclib_id, actor) -> AdminLyricsDecisionResult:
        # RETURNING, not rowcount: whether the row is new is the idempotency answer, and a
        # DO NOTHING conflict returns no row.
        inserted = db.execute(
            pg_insert(TrackLyricsRejection)
            .values(track_id=track_id, lrclib_id=lrclib_id, rejected_by=actor)
            .on_conflict_do_nothing(index_elements=["track_id", "lrclib_id"])
            .returning(TrackLyricsRejection.lrclib_id)
        ).first() is not None
        serving = (
            row is not None
            and row.match_status in ("matched", "no_lyrics")
            and _current_lrclib_id(row) == lrclib_id
        )
        if serving:
            # Take the rejected lyric down now. The row leaves the lock too: the owner's newer
            # verdict replaces the older one, and an unresolved row is the queue's to fill.
            row.evidence = {
                "reason": "owner_rejected",
                "rejected_lrclib_id": lrclib_id,
                "manual": _stamp("reject", actor, row),
            }
            row.match_status = "review_required"
            row.lyric_plain = None
            row.lyric_synced = None
            row.updated_at = datetime.now(timezone.utc)
        resolved = resolve_open_reports(db, track_id, "reject", actor) if serving else 0
        db.commit()
        current = self._settled_view(db, row)
        if not inserted and not serving:
            return AdminLyricsDecisionResult(action="reject", outcome="no_change", current=current)
        logger.warning("Lyrics panel: LRCLIB %s rejected for track %s by %s (served=%s)",
                       lrclib_id, track_id, actor, serving)
        sent = self._recheck(track_id) if serving else False
        return AdminLyricsDecisionResult(
            action="reject", outcome="applied", recheck_enqueued=sent,
            reports_resolved=resolved, current=current,
        )

    def _keep(self, db, track_id, row, actor) -> AdminLyricsDecisionResult:
        resolved = resolve_open_reports(db, track_id, "keep", actor)
        if not resolved:
            current = current_view(row)
            db.rollback()
            return AdminLyricsDecisionResult(action="keep", outcome="no_change", current=current)
        db.commit()
        current = self._settled_view(db, row)
        logger.warning("Lyrics panel: %d report(s) on track %s closed as kept by %s",
                       resolved, track_id, actor)
        return AdminLyricsDecisionResult(
            action="keep", outcome="applied", reports_resolved=resolved, current=current,
        )

    def _unlock(self, db, track_id, row, actor) -> AdminLyricsDecisionResult:
        lock = _lock_of(row)
        if lock is None:
            current = current_view(row)
            db.rollback()
            return AdminLyricsDecisionResult(action="unlock", outcome="no_change", current=current)
        evidence = {k: v for k, v in (row.evidence or {}).items() if k != "manual_fix"}
        evidence["match_basis"] = BASIS_UNLOCKED
        evidence["unlocked"] = _stamp("unlock", actor, row)
        row.evidence = evidence
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        current = self._settled_view(db, row)
        logger.warning("Lyrics panel: track %s unlocked (%s) by %s", track_id, lock, actor)
        return AdminLyricsDecisionResult(
            action="unlock", outcome="applied", recheck_enqueued=self._recheck(track_id),
            current=current,
        )

    @staticmethod
    def _settled_view(db: Session, row: Optional[TrackLyrics]) -> AdminLyricsCurrent:
        """The committed row as the response shows it. Reading it back opens a new read
        transaction; end it here, so no SQS send that follows runs inside one."""
        if row is not None:
            db.refresh(row)
        current = current_view(row)
        db.rollback()
        return current

    @staticmethod
    def _resolve_after_send(db: Session, track_id, resolution: str, actor: str) -> int:
        """Best effort after a successful send: the decision is already in flight, so a DB
        error here leaves the reports open for the owner to close again, not a 503."""
        try:
            resolved = resolve_open_reports(db, track_id, resolution, actor)
            db.commit()
            return resolved
        except Exception as error:  # noqa: BLE001
            db.rollback()
            logger.error("Lyrics panel: reports on %s not closed after %s (%s)",
                         track_id, resolution, type(error).__name__)
            return 0

    def _recheck(self, track_id) -> bool:
        """Best effort after a committed decision: the decision stands either way. A taken-down
        row is unresolved, so the scheduled queue re-checks it regardless; an unlocked row keeps
        serving its body until the next re-check reaches it (the panel's refresh, or the
        re-sweep after a matcher bump)."""
        try:
            return self.sqs.send_lyrics_recheck(str(track_id))
        except Exception as error:  # noqa: BLE001
            logger.error("Lyrics panel: re-check not enqueued for %s (%s)", track_id, type(error).__name__)
            return False
