"""Owner automation snapshot (FEAT-automation-admin 2b).

Three sources, each with its own `observed_at`:

* the static catalog (`automation_catalog`) — what each job is;
* the DB adapter — per-queue counts by state, oldest waiting, last update;
* local-agent heartbeats (`automation_heartbeats`, V59) — liveness by age.

The AWS adapter is 2d. Until then every AWS row renders unknown, and so does
anything else without fresh evidence: a missing source must never read as
healthy or as an empty queue.

Shape (CLAUDE.md, recurring bug class): fetch everything the DB has to say in
one short read-only transaction → close the session → (2d: AWS reads) →
assemble. Nothing external ever runs while a session is open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.schemas import (
    AutomationHeartbeatView,
    AutomationJob,
    AutomationLiveness,
    AutomationQueue,
    AutomationQueueDetail,
    AutomationQueueFlow,
    AutomationReasonCount,
    AutomationSnapshot,
    AutomationSource,
    AutomationWaitingItem,
)
from app.services import automation_catalog as catalog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueRead:
    counts: Dict[str, int]
    oldest_waiting_at: Optional[datetime]
    last_update_at: Optional[datetime]
    stale_running: Optional[int]


@dataclass(frozen=True)
class HeartbeatRow:
    agent_key: str
    observed_at: datetime
    enabled: Optional[bool]
    last_outcome: Optional[str]
    details: Dict[str, Any]


@dataclass(frozen=True)
class DbRead:
    observed_at: datetime  # the DB's now(), so heartbeat age needs no Lambda clock
    queues: Dict[str, QueueRead]  # QueueDef.key / translation_queue_key(version)
    heartbeats: List[HeartbeatRow]


def _fold(rows: Iterable[Tuple[Any, ...]]) -> QueueRead:
    """Fold (state, n, oldest_waiting, last_update, stale_running) rows."""
    counts: Dict[str, int] = {}
    oldest: Optional[datetime] = None
    last: Optional[datetime] = None
    stale: Optional[int] = None
    for state, n, row_oldest, row_last, row_stale in rows:
        counts[str(state)] = int(n)
        if row_oldest is not None and (oldest is None or row_oldest < oldest):
            oldest = row_oldest
        if row_last is not None and (last is None or row_last > last):
            last = row_last
        if row_stale is not None:
            stale = (stale or 0) + int(row_stale)
    return QueueRead(counts, oldest, last, stale)


def read_db_state(db: Session) -> DbRead:
    """Every DB fact the snapshot needs. SELECTs only; the caller owns the session."""
    observed_at = db.execute(text("SELECT now()")).scalar_one()

    queues: Dict[str, QueueRead] = {}
    for q in catalog.QUEUES:
        queues[q.key] = _fold(db.execute(text(q.sql)).all())

    by_version: Dict[str, List[Tuple[Any, ...]]] = {}
    for version, *rest in db.execute(text(catalog.TRANSLATION_WORK_SQL)).all():
        by_version.setdefault(str(version), []).append(tuple(rest))
    for version, rows in by_version.items():
        queues[catalog.translation_queue_key(version)] = _fold(rows)

    heartbeats = [
        HeartbeatRow(
            agent_key=r[0],
            observed_at=r[1],
            enabled=r[2],
            last_outcome=r[3],
            details=r[4] if isinstance(r[4], dict) else {},
        )
        for r in db.execute(text(catalog.HEARTBEATS_SQL)).all()
    ]
    return DbRead(observed_at=observed_at, queues=queues, heartbeats=heartbeats)


# ── assembly (pure) ─────────────────────────────────────────────────────────────

FUTURE_TOLERANCE_SECONDS = 5

_UNKNOWN_BY_GROUP = {
    "aws_schedule": "AWS adapter not wired yet (FEAT-automation-admin 2d).",
    "aws_scheduler": "AWS adapter not wired yet (FEAT-automation-admin 2d).",
    "aws_queue_message": "SQS keeps no per-message history (FEAT-durable-job-status).",
    "other_scheduler": "No adapter reads GitHub Actions.",
}


def _age(now: datetime, observed_at: datetime) -> int:
    return int((now - observed_at).total_seconds())


def _liveness(
    job: catalog.JobDef,
    db_read: Optional[DbRead],
    heartbeats: Dict[str, HeartbeatRow],
) -> AutomationLiveness:
    if job.heartbeat_key is None or job.stale_after_seconds is None:
        return AutomationLiveness(status="unknown", reason=_UNKNOWN_BY_GROUP.get(job.group, "No adapter."))
    if db_read is None:
        return AutomationLiveness(status="unknown", reason="Heartbeat source unavailable.")
    hb = heartbeats.get(job.heartbeat_key)
    if hb is None:
        return AutomationLiveness(status="unknown", reason="No heartbeat reported.")
    age = _age(db_read.observed_at, hb.observed_at)
    if -FUTURE_TOLERANCE_SECONDS <= age < 0:
        # A writer that committed after this read's now() was taken (READ
        # COMMITTED: each statement sees newer commits). That is a fresh row.
        age = 0
    if age < 0:
        # Writers set observed_at = now() on the DB side; a future value means
        # one did not, and its freshness proves nothing.
        return AutomationLiveness(
            status="unknown", observed_at=hb.observed_at, age_seconds=age,
            reason="Heartbeat observed_at is in the future.",
        )
    if age > job.stale_after_seconds:
        return AutomationLiveness(
            status="stale", observed_at=hb.observed_at, age_seconds=age,
            reason=f"Last heartbeat is older than the {job.stale_after_seconds}s bound.",
        )
    return AutomationLiveness(
        status="fresh", observed_at=hb.observed_at, age_seconds=age,
        reason=f"Heartbeat within the {job.stale_after_seconds}s bound.",
    )


def _queue(
    key: str, table: str, label: str, frozen: bool, waiting: Tuple[str, ...], db_read: Optional[DbRead]
) -> AutomationQueue:
    if db_read is None:
        return AutomationQueue(
            key=key, table=table, label=label, frozen=frozen,
            waiting_states=list(waiting), status="unknown",
        )
    # The read succeeded; a missing key means the table has no such rows.
    read = db_read.queues.get(key) or QueueRead({}, None, None, None)
    return AutomationQueue(
        key=key, table=table, label=label, frozen=frozen, waiting_states=list(waiting),
        status="ok", observed_at=db_read.observed_at, counts=dict(read.counts),
        oldest_waiting_at=read.oldest_waiting_at, last_update_at=read.last_update_at,
        stale_running=read.stale_running,
    )


def _queues(db_read: Optional[DbRead]) -> List[AutomationQueue]:
    out = [
        _queue(q.key, q.table, q.label, q.frozen, q.waiting_states, db_read)
        for q in catalog.QUEUES
    ]
    known = {v.version for v in catalog.TRANSLATION_VERSIONS}
    versions = list(catalog.TRANSLATION_VERSIONS)
    if db_read is not None:
        prefix = f"{catalog.TRANSLATION_WORK_TABLE}:"
        found = sorted(
            k[len(prefix):] for k in db_read.queues
            if k.startswith(prefix) and k[len(prefix):] not in known
        )
        versions += [catalog.TranslationVersion(v, f"Translation work ({v})", frozen=False) for v in found]
    for v in versions:
        out.append(_queue(
            catalog.translation_queue_key(v.version), catalog.TRANSLATION_WORK_TABLE,
            v.label, v.frozen, catalog.TRANSLATION_WAITING_STATES, db_read,
        ))
    return out


def assemble(db_read: Optional[DbRead], *, fallback_now: datetime) -> AutomationSnapshot:
    now = db_read.observed_at if db_read is not None else fallback_now
    heartbeats = {h.agent_key: h for h in (db_read.heartbeats if db_read else [])}
    catalogued_keys = {j.heartbeat_key for j in catalog.JOBS if j.heartbeat_key}

    jobs = [
        AutomationJob(
            key=j.key, group=j.group, name=j.name, purpose=j.purpose, trigger=j.trigger,
            timezone=j.timezone, executor=j.executor, location=j.location,
            declared_state=j.declared_state,
            drains=list(j.drains), queue_keys=list(j.queue_keys), ai_provider=j.ai_provider,
            note=j.note, controls=list(j.controls), heartbeat_key=j.heartbeat_key,
            stale_after_seconds=j.stale_after_seconds,
            liveness=_liveness(j, db_read, heartbeats),
        )
        for j in catalog.JOBS
    ]

    db_status: Literal["ok", "unavailable"] = "ok" if db_read is not None else "unavailable"
    db_detail = None if db_read is not None else "Database read failed."
    sources = [
        AutomationSource(key="catalog", status="ok", detail=f"Step 1 inventory as of {catalog.CATALOG_AS_OF.isoformat()}."),
        AutomationSource(key="db", status=db_status, observed_at=db_read.observed_at if db_read else None, detail=db_detail),
        AutomationSource(
            key="heartbeat", status=db_status, observed_at=db_read.observed_at if db_read else None,
            detail=db_detail if db_read is None else f"{len(db_read.heartbeats)} agent(s) reporting.",
        ),
        AutomationSource(key="aws", status="not_wired", detail="FEAT-automation-admin 2d."),
    ]

    return AutomationSnapshot(
        generated_at=now,
        catalog_as_of=catalog.CATALOG_AS_OF,
        sources=sources,
        jobs=jobs,
        queues=_queues(db_read),
        heartbeats=[
            AutomationHeartbeatView(
                agent_key=h.agent_key, observed_at=h.observed_at, age_seconds=_age(now, h.observed_at),
                enabled=h.enabled, last_outcome=h.last_outcome, details=h.details,
                catalogued=h.agent_key in catalogued_keys,
            )
            for h in heartbeats.values()
        ],
    )


# ── queue detail (2f) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DetailTarget:
    key: str
    table: str
    label: str
    frozen: bool
    waiting_states: Tuple[str, ...]
    definition: catalog.QueueDetailDef
    params: Dict[str, Any]


def resolve_queue(key: str) -> Optional[DetailTarget]:
    """Map a snapshot queue key to its detail reads; None for anything else.

    Only keys the snapshot itself can produce resolve: the fixed queues, and
    `lyrics_translation_work:<version>` for any version (the snapshot lists
    versions the catalog has not heard of, so their detail must open too).
    The version reaches SQL only as a bind parameter.
    """
    for q in catalog.QUEUES:
        if q.key == key:
            return DetailTarget(q.key, q.table, q.label, q.frozen, q.waiting_states,
                                catalog.QUEUE_DETAILS[q.key], {})
    prefix = f"{catalog.TRANSLATION_WORK_TABLE}:"
    if not key.startswith(prefix):
        return None
    version = key[len(prefix):]
    if not version.strip() or len(version) > catalog.MAX_TRANSLATOR_VERSION_LENGTH:
        return None
    known = {v.version: v for v in catalog.TRANSLATION_VERSIONS}
    v = known.get(version) or catalog.TranslationVersion(version, f"Translation work ({version})", frozen=False)
    return DetailTarget(key, catalog.TRANSLATION_WORK_TABLE, v.label, v.frozen,
                        catalog.TRANSLATION_WAITING_STATES, catalog.TRANSLATION_WORK_DETAIL,
                        {"version": version})


@dataclass(frozen=True)
class DetailRead:
    observed_at: datetime
    reasons: List[Tuple[Any, ...]]
    flow: Tuple[Any, ...]
    oldest: List[Tuple[Any, ...]]


def read_queue_detail(db: Session, target: DetailTarget) -> DetailRead:
    """SELECTs only; the caller owns the session (and its read-only transaction)."""
    d = target.definition
    observed_at = db.execute(text("SELECT now()")).scalar_one()
    reasons = [tuple(r) for r in db.execute(text(d.reasons_sql), target.params).all()]
    flow = tuple(db.execute(text(d.flow_sql), target.params).one())
    oldest = [
        tuple(r) for r in db.execute(
            text(d.oldest_sql), {**target.params, "limit": catalog.OLDEST_WAITING_LIMIT}
        ).all()
    ]
    return DetailRead(observed_at=observed_at, reasons=reasons, flow=flow, oldest=oldest)


def _opt_int(v: Any) -> Optional[int]:
    return None if v is None else int(v)


def assemble_detail(target: DetailTarget, read: Optional[DetailRead]) -> AutomationQueueDetail:
    d = target.definition
    base = dict(
        key=target.key, table=target.table, label=target.label, frozen=target.frozen,
        waiting_states=list(target.waiting_states), reasons_note=d.reasons_note,
        wait_basis=d.wait_basis, oldest_limit=catalog.OLDEST_WAITING_LIMIT,
    )
    if read is None:
        return AutomationQueueDetail(status="unknown", **base)
    reasons = sorted(
        (
            AutomationReasonCount(state=str(state), reason=reason, count=int(n), deferred=_opt_int(deferred))
            for state, reason, n, deferred in read.reasons
        ),
        key=lambda r: (-r.count, r.state, r.reason or ""),
    )
    entered, left = read.flow
    return AutomationQueueDetail(
        status="ok", observed_at=read.observed_at, reasons=reasons,
        flow=AutomationQueueFlow(
            window_hours=catalog.FLOW_WINDOW_HOURS, entered=_opt_int(entered), left=_opt_int(left),
            entered_basis=d.entered_basis, left_basis=d.left_basis,
        ),
        oldest_waiting=[
            AutomationWaitingItem(
                id=str(item_id), title=title, state=str(state), reason=reason,
                attempts=_opt_int(attempts), waiting_since=since, next_attempt_at=next_at,
            )
            for item_id, title, state, reason, attempts, since, next_at in read.oldest
        ],
        **base,
    )


class AutomationService:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    def snapshot(self) -> AutomationSnapshot:
        db_read: Optional[DbRead] = None
        try:
            with self._session_factory() as db:
                # Transaction-scoped (not session-scoped), so it cannot leak through
                # the Neon pooler to the next borrower of this connection.
                db.execute(text("SET TRANSACTION READ ONLY"))
                db_read = read_db_state(db)
            # Session closed (and its transaction rolled back) here.
        except SQLAlchemyError:
            logger.exception("automation snapshot: DB read failed")
        # 2d: AWS reads go here — after the session above is closed.
        return assemble(db_read, fallback_now=datetime.now(timezone.utc))

    def queue_detail(self, key: str) -> Optional[AutomationQueueDetail]:
        """None when `key` is not a queue the snapshot can show (the route's 404)."""
        target = resolve_queue(key)
        if target is None:
            return None
        read: Optional[DetailRead] = None
        try:
            with self._session_factory() as db:
                db.execute(text("SET TRANSACTION READ ONLY"))
                read = read_queue_detail(db, target)
        except SQLAlchemyError:
            logger.exception("automation queue detail: DB read failed")
        return assemble_detail(target, read)
