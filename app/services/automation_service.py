"""Owner automation snapshot (FEAT-automation-admin 2b).

Three sources, each with its own `observed_at`:

* the static catalog (`automation_catalog`) — what each job is;
* the DB adapter — per-queue counts by state, oldest waiting, last update;
* local-agent heartbeats (`automation_heartbeats`, V59) — liveness by age;
* the AWS adapter (`automation_aws`, 2d) — EventBridge rule state and delivery
  metrics, queue depth, Lambda errors, alarm state.

Anything without fresh evidence renders unknown: a missing source must never
read as healthy or as an empty queue.

Shape (CLAUDE.md, recurring bug class): fetch everything the DB has to say in
one short read-only transaction → close the session → AWS reads → assemble.
Nothing external ever runs while a session is open.
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
    AutomationAws,
    AutomationAwsAlarm,
    AutomationAwsFunction,
    AutomationAwsQueue,
    AutomationAwsRule,
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
from app.services.automation_aws import AwsRead

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
    "aws_schedule": "No AWS reader is configured.",
    "aws_scheduler": "EventBridge Scheduler is outside the backend's read grant (FEAT-automation-admin 2d).",
    "aws_queue_message": "SQS keeps no per-message history (FEAT-durable-job-status).",
    "other_scheduler": "No adapter reads GitHub Actions.",
}


def _age(now: datetime, observed_at: datetime) -> int:
    return int((now - observed_at).total_seconds())


_RULES_BY_NAME = {r.name: r for r in catalog.AWS_RULES}

# EventBridge invokes the worker asynchronously: a delivered tick whose handler
# then throws still counts as delivered. Handler failures are worker-wide (the
# blogWorkerLambda-errors alarm, worker-cron-dlq), not per rule — so say so.
DELIVERY_ONLY = "Delivery only: whether the handler succeeded is not per-rule evidence."


def _hours(seconds: int) -> str:
    h = seconds / 3600
    return f"{h:g} h" if h < 48 else f"{h / 24:g} days"


def _rule_liveness(job: catalog.JobDef, aws: Optional[AwsRead]) -> AutomationLiveness:
    """EventBridge rows. Evidence: the rule's state, CloudWatch's delivery
    counts, and the rule's FailedInvocations alarm. CloudWatch records every
    delivery, so a successful metric read with no datapoints IS evidence (no
    delivery), unlike a heartbeat that was never written."""
    rule = _RULES_BY_NAME.get(job.aws_rule or "")
    if aws is None or rule is None:
        return AutomationLiveness(status="unknown", reason=_UNKNOWN_BY_GROUP["aws_schedule"])
    state = aws.rules.get(rule.name) if aws.rules_section.ok else None
    metrics = aws.rule_metrics.get(rule.name) if aws.metrics_section.ok else None
    if state is None or metrics is None:
        missing = [n for n, v in (("rule state", state), ("delivery metrics", metrics)) if v is None]
        return AutomationLiveness(status="unknown", reason=f"AWS {' and '.join(missing)} not read.")
    now = aws.metrics_section.observed_at
    seen = metrics.last_delivery_at
    age = _age(now, seen) if (now is not None and seen is not None) else None

    alarm_state = None
    if rule.alarm is not None and aws.alarms_section.ok:
        alarm = aws.alarms.get(rule.alarm)
        alarm_state = alarm.state if alarm is not None else None
    counts = f"{metrics.delivered_24h} delivered, {metrics.failed_24h} failed in 24 h"
    if metrics.failed_in_lookback > metrics.failed_24h:
        counts += f" ({metrics.failed_in_lookback} failed in {_hours(metrics.lookback_seconds)})"
    if rule.alarm is None:
        alarm_note = ""
    elif alarm_state is None:
        alarm_note = "; alarm not read"
    else:
        alarm_note = f"; alarm {alarm_state}"

    if not (state.state or "").startswith("ENABLED"):
        return AutomationLiveness(status="stale", observed_at=seen, age_seconds=age,
                                  reason=f"Rule is {state.state or 'in an unknown state'}; {counts}.")
    # Failing = failing NOW: the newest attempt failed, the alarm is firing, or a
    # failure falls inside the rule's own staleness bound (about its last two
    # ticks). One transient failure must not keep a 5-minute rule red for a day
    # after hundreds of good ticks; older failures stay in the reason text.
    recent_failure = (
        metrics.last_failure_at is not None and now is not None
        and _age(now, metrics.last_failure_at) <= rule.stale_after_seconds
    )
    if metrics.last_attempt_failed or recent_failure or alarm_state == "ALARM":
        return AutomationLiveness(status="failing", observed_at=seen, age_seconds=age,
                                  reason=f"AWS reports failed deliveries: {counts}{alarm_note}.")
    if seen is None or age is None:
        return AutomationLiveness(status="stale",
                                  reason=f"No delivery in the last {_hours(metrics.lookback_seconds)}{alarm_note}.")
    if age > rule.stale_after_seconds:
        return AutomationLiveness(status="stale", observed_at=seen, age_seconds=age,
                                  reason=f"Last delivery is older than the {rule.stale_after_seconds}s bound; {counts}{alarm_note}.")
    return AutomationLiveness(status="fresh", observed_at=seen, age_seconds=max(age, 0),
                              reason=f"Delivered within the {rule.stale_after_seconds}s bound; {counts}{alarm_note}. {DELIVERY_ONLY}")


def _liveness(
    job: catalog.JobDef,
    db_read: Optional[DbRead],
    heartbeats: Dict[str, HeartbeatRow],
    aws: Optional[AwsRead] = None,
) -> AutomationLiveness:
    if job.aws_rule is not None:
        return _rule_liveness(job, aws)
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


def _aws_view(aws: AwsRead) -> AutomationAws:
    def stamp(section, present: bool) -> Dict[str, Any]:
        if section.ok and present:
            return {"status": "ok", "observed_at": section.observed_at}
        return {"status": "unknown"}

    rules = []
    for r in catalog.AWS_RULES:
        st = aws.rules.get(r.name) if aws.rules_section.ok else None
        m = aws.rule_metrics.get(r.name) if aws.metrics_section.ok else None
        # One item from two reads: ok only when both arrived; the observed_at is
        # the later read, and each half's numbers stay null if it is missing.
        both = st is not None and m is not None
        observed = max(t for t in (aws.rules_section.observed_at, aws.metrics_section.observed_at) if t) if both else None
        rules.append(AutomationAwsRule(
            job_key=r.job_key, rule=r.name, status="ok" if both else "unknown", observed_at=observed,
            state=st.state if st else None, schedule=st.schedule if st else None,
            last_delivery_at=m.last_delivery_at if m else None,
            delivered_24h=m.delivered_24h if m else None, failed_24h=m.failed_24h if m else None,
            lookback_hours=(m.lookback_seconds // 3600) if m else None, alarm=r.alarm,
            last_attempt_failed=m.last_attempt_failed if m else None,
        ))
    queues = []
    for q in catalog.AWS_QUEUES:
        d = aws.queues.get(q.name) if aws.queues_section.ok else None
        queues.append(AutomationAwsQueue(
            name=q.name, role=q.role, label=q.label, **stamp(aws.queues_section, d is not None),
            visible=d.visible if d else None, in_flight=d.in_flight if d else None, delayed=d.delayed if d else None,
        ))
    functions = []
    for f in catalog.AWS_FUNCTIONS:
        m = aws.function_metrics.get(f.name) if aws.metrics_section.ok else None
        functions.append(AutomationAwsFunction(
            name=f.name, label=f.label, **stamp(aws.metrics_section, m is not None),
            invocations_24h=m.invocations_24h if m else None, errors_24h=m.errors_24h if m else None,
            throttles_24h=m.throttles_24h if m else None,
        ))
    alarms = []
    for name in catalog.AWS_ALARMS:
        a = aws.alarms.get(name) if aws.alarms_section.ok else None
        alarms.append(AutomationAwsAlarm(
            name=name, **stamp(aws.alarms_section, a is not None),
            state=a.state if a else None, state_updated_at=a.updated_at if a else None,
        ))
    return AutomationAws(rules=rules, queues=queues, functions=functions, alarms=alarms)


def _aws_source(aws: Optional[AwsRead]) -> AutomationSource:
    if aws is None:
        return AutomationSource(key="aws", status="not_wired", detail="No AWS reader is configured.")
    sections = (("rules", aws.rules_section), ("metrics", aws.metrics_section),
                ("queues", aws.queues_section), ("alarms", aws.alarms_section))
    ok = [s for _, s in sections if s.ok]
    notes = [f"{name}: {s.detail}" for name, s in sections if s.detail]
    detail = " ".join(notes) or f"{len(ok)} of {len(sections)} reads succeeded."
    if not ok:
        return AutomationSource(key="aws", status="unavailable", detail=detail)
    return AutomationSource(key="aws", status="ok", observed_at=max(s.observed_at for s in ok if s.observed_at), detail=detail)


def assemble(db_read: Optional[DbRead], *, fallback_now: datetime, aws: Optional[AwsRead] = None) -> AutomationSnapshot:
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
            liveness=_liveness(j, db_read, heartbeats, aws),
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
        _aws_source(aws),
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
        aws=_aws_view(aws) if aws is not None else None,
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
    def __init__(
        self,
        session_factory: Callable[[], Session],
        aws_reader: Optional[Callable[[], AwsRead]] = None,
    ):
        self._session_factory = session_factory
        # None = AWS not wired (tests, or a deployment without a reader): every
        # AWS row stays unknown. The reader itself never raises.
        self._aws_reader = aws_reader

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
        # AWS reads only now, after the session above is closed.
        aws = self._aws_reader() if self._aws_reader is not None else None
        return assemble(db_read, fallback_now=datetime.now(timezone.utc), aws=aws)

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
