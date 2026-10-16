"""Owner automation snapshot (FEAT-automation-admin 2b).

Three sources, each with its own `observed_at`:

* the static catalog (`automation_catalog`) — what each job is;
* the DB adapter — per-queue counts by state, oldest waiting, last update;
* local-agent heartbeats (`automation_heartbeats`, V59) — liveness by age;
* the AWS adapter (`automation_aws`, 2d) — EventBridge rule state and delivery
  metrics, queue depth, Lambda errors, alarm state;
* owner pause flags (`automation_controls`, V60, Step 3b) — read with the rest;
* owner run-now rows (`automation_control_events`, V62, Step 3c) — likewise.

The write paths are `AutomationService.control` (3b: pause/resume) and
`AutomationService.run` (3c: run now), both audited and idempotent by a
client-minted `request_id`. `run` is also the one place an external call is made
on a request: an async Lambda invoke, with no DB session open across it.

Anything without fresh evidence renders unknown: a missing source must never
read as healthy or as an empty queue.

Shape (CLAUDE.md, recurring bug class): fetch everything the DB has to say in
one short read-only transaction → close the session → AWS reads → assemble.
Nothing external ever runs while a session is open.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    AutomationControlResult,
    AutomationControlState,
    AutomationHeartbeatView,
    AutomationJob,
    AutomationLiveness,
    AutomationQueue,
    AutomationQueueDetail,
    AutomationQueueFlow,
    AutomationReasonCount,
    AutomationRunResult,
    AutomationRunState,
    AutomationRunView,
    AutomationSnapshot,
    AutomationSource,
    AutomationWaitingItem,
)
from app.services import automation_catalog as catalog
from app.services.automation_aws import AwsRead
from app.services.automation_invoke import Invoker

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
class ControlRow:
    paused: bool
    changed_at: datetime
    honored_at: Optional[datetime]


@dataclass(frozen=True)
class RunRow:
    """The job's latest run-now row, and the start of its cooldown (3c)."""
    request_id: Any
    outcome: str
    requested_at: datetime
    invoked_at: Optional[datetime]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    run_outcome: Optional[str]
    cooldown_from: Optional[datetime]


@dataclass(frozen=True)
class DbRead:
    observed_at: datetime  # the DB's now(), so heartbeat age needs no Lambda clock
    queues: Dict[str, QueueRead]  # QueueDef.key / translation_queue_key(version)
    heartbeats: List[HeartbeatRow]
    controls: Dict[str, ControlRow] = field(default_factory=dict)
    runs: Dict[str, RunRow] = field(default_factory=dict)


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
    controls = {
        r[0]: ControlRow(paused=bool(r[1]), changed_at=r[2], honored_at=r[3])
        for r in db.execute(text(catalog.CONTROLS_SQL)).all()
    }
    runs = {r[0]: RunRow(*r[1:]) for r in db.execute(text(catalog.RUNS_SQL)).all()}
    return DbRead(observed_at=observed_at, queues=queues, heartbeats=heartbeats, controls=controls, runs=runs)


def control_state(job: catalog.JobDef, row: Optional[ControlRow], now: datetime, *, read_ok: bool = True) -> AutomationControlState:
    """A job's pause state. No row = never controlled = not paused. A failed read is
    unknown (`paused` None), never "running"."""
    if not read_ok:
        return AutomationControlState()
    if row is None:
        return AutomationControlState(paused=False)
    confirmed = row.paused and row.honored_at is not None and row.honored_at >= row.changed_at
    # Before one schedule interval has passed, "not confirmed yet" is expected.
    grace = job.interval_seconds or job.stale_after_seconds or 0
    overdue = row.paused and not confirmed and (now - row.changed_at).total_seconds() > grace
    return AutomationControlState(
        paused=row.paused, changed_at=row.changed_at, honored_at=row.honored_at,
        confirmed=confirmed, confirmation_overdue=overdue,
    )


def run_state(row: Optional[RunRow], now: datetime, *, read_ok: bool = True) -> AutomationRunState:
    """A job's run-now state (3c). No row = never run from the page. A failed read is
    unknown, never "can run now"."""
    if not read_ok:
        return AutomationRunState(status="unknown")
    if row is None:
        return AutomationRunState(status="ok")
    cooldown = timedelta(seconds=catalog.RUN_COOLDOWN_SECONDS)
    until = row.cooldown_from + cooldown if row.cooldown_from is not None else None
    stalled = row.started_at is not None and row.finished_at is None and now - row.started_at > cooldown
    return AutomationRunState(
        status="ok",
        last=AutomationRunView(
            request_id=row.request_id, outcome=row.outcome, requested_at=row.requested_at,
            invoked_at=row.invoked_at, started_at=row.started_at, finished_at=row.finished_at,
            run_outcome=row.run_outcome,
        ),
        cooldown_until=until if until is not None and until > now else None,
        stalled=stalled,
    )


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
            control=(
                control_state(j, db_read.controls.get(j.key) if db_read else None, now, read_ok=db_read is not None)
                if "pause" in j.controls else None
            ),
            run=(
                run_state(db_read.runs.get(j.key) if db_read else None, now, read_ok=db_read is not None)
                if "run" in j.controls else None
            ),
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
        invoker: Optional[Invoker] = None,
    ):
        self._session_factory = session_factory
        # None = AWS not wired (tests, or a deployment without a reader): every
        # AWS row stays unknown. The reader itself never raises.
        self._aws_reader = aws_reader
        # None = run-now not wired: every run records invoke_failed. The invoker
        # itself never raises (automation_invoke).
        self._invoker = invoker

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

    def control(self, job_key: str, action: str, request_id: Any, actor: str) -> Optional[AutomationControlResult]:
        """Pause or resume one job (3b). None when the key has no pause control (the
        route's 404). Raises ControlConflict for a reused request_id, and lets a DB
        error propagate (the route answers 503) — a control must never report a
        change it did not make."""
        job = _job(job_key)
        if job is None:
            return None
        with self._session_factory() as db:
            result = _apply_control(db, job, action, request_id, actor)
        logger.info(
            "automation control: %s %s → %s%s", action, job.key, result.outcome,
            " (replay)" if result.replayed else "",
        )
        return result

    def run(self, job_key: str, request_id: Any, actor: str) -> Optional[AutomationRunResult]:
        """Run one worker cron now (3c). None when the key has no run control (the
        route's 404). Raises RunRejected (409: paused / cooldown), ControlConflict
        (409: request_id reused), or lets a DB error propagate (503).

        Shape: claim in one short write transaction → close it → invoke → record the
        answer in a fresh short write. Never a session open across the invoke."""
        job = _runnable(job_key)
        if job is None:
            return None
        with self._session_factory() as db:
            replay = _claim_run(db, job, request_id, actor)
        if replay is not None:
            logger.info("automation run: %s → %s (replay)", job.key, replay.outcome)
            return replay
        accepted = self._invoker(job.key, request_id) if self._invoker is not None else False
        outcome = "invoked" if accepted else "invoke_failed"
        with self._session_factory() as db:
            row, now = _record_invoke(db, request_id, outcome)
        logger.info("automation run: %s → %s", job.key, row.outcome)
        return AutomationRunResult(
            request_id=request_id, job_key=job.key, outcome=row.outcome, replayed=False,
            run=run_state(row, now),
        )

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


# ── owner controls (3b) ─────────────────────────────────────────────────────────


class ControlConflict(Exception):
    """The request_id was already used for a different job or action."""


_LOCK_CONTROL = text(
    # Creates the row on first use and takes its row lock in the same statement.
    "INSERT INTO automation_controls (job_key) VALUES (:key) "
    "ON CONFLICT (job_key) DO UPDATE SET job_key = EXCLUDED.job_key "
    "RETURNING paused, changed_at, honored_at, now()"
)
# clock_timestamp(), not now(): now() is the transaction's START, and a request that
# waited on the row lock would then sort before the one it waited for — the audit
# trail must read in the order the actions took effect.
_RECORD_EVENT = text(
    "INSERT INTO automation_control_events (request_id, job_key, action, outcome, actor_sub, created_at) "
    "VALUES (:rid, :key, :action, :outcome, :actor, clock_timestamp()) "
    "ON CONFLICT (request_id) DO NOTHING RETURNING request_id"
)
_APPLY = text(
    "UPDATE automation_controls SET paused = :paused, changed_at = clock_timestamp() "
    "WHERE job_key = :key RETURNING paused, changed_at, honored_at, now()"
)


def _job(key: str) -> Optional[catalog.JobDef]:
    return next((j for j in catalog.JOBS if j.key == key and "pause" in j.controls), None)


def _apply_control(db: Session, job: catalog.JobDef, action: str, request_id: Any, actor: str) -> AutomationControlResult:
    """One short write transaction; every caller takes the control row lock, then the
    event key, so two requests can never wait on each other in a cycle."""
    db.execute(text("SET TRANSACTION READ WRITE"))  # a pooler session can leak read-only
    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    db.execute(text("SET LOCAL statement_timeout = '5s'"))
    want = action == "pause"
    paused, changed_at, honored_at, now = db.execute(_LOCK_CONTROL, {"key": job.key}).one()
    outcome = "no_change" if paused == want else "applied"
    recorded = db.execute(_RECORD_EVENT, {
        "rid": request_id, "key": job.key, "action": action, "outcome": outcome, "actor": actor,
    }).first()
    if recorded is None:
        # Already processed (an earlier send, or a concurrent one that committed while
        # this waited). Act on nothing: report the stored outcome with the CURRENT
        # state — the row this transaction just locked, which nothing changed since.
        stored = db.execute(
            text("SELECT job_key, action, outcome FROM automation_control_events WHERE request_id = :rid"),
            {"rid": request_id},
        ).one()
        db.rollback()  # drops the row this request may have created on first use
        if stored[0] != job.key or stored[1] != action:
            raise ControlConflict()
        row = ControlRow(paused=bool(paused), changed_at=changed_at, honored_at=honored_at)
        return AutomationControlResult(
            request_id=request_id, job_key=job.key, action=action, outcome=stored[2], replayed=True,
            control=control_state(job, row, now),
        )
    if outcome == "applied":
        paused, changed_at, honored_at, now = db.execute(_APPLY, {"key": job.key, "paused": want}).one()
    db.commit()
    row = ControlRow(paused=bool(paused), changed_at=changed_at, honored_at=honored_at)
    return AutomationControlResult(
        request_id=request_id, job_key=job.key, action=action, outcome=outcome, replayed=False,
        control=control_state(job, row, now),
    )


# ── owner run now (3c) ──────────────────────────────────────────────────────────


class RunRejected(Exception):
    """The job may not run now: `code` is job_paused or cooldown."""

    def __init__(self, code: str, cooldown_until: Optional[datetime] = None, retry_after_seconds: Optional[int] = None):
        super().__init__(code)
        self.code = code
        self.cooldown_until = cooldown_until
        self.retry_after_seconds = retry_after_seconds


def _runnable(key: str) -> Optional[catalog.JobDef]:
    return next((j for j in catalog.JOBS if j.key == key and "run" in j.controls), None)


_RUN_COLUMNS = "request_id, outcome, created_at, invoked_at, started_at, finished_at, run_outcome"

_STORED_EVENT = text(
    f"SELECT job_key, action, {_RUN_COLUMNS} FROM automation_control_events WHERE request_id = :rid"
)
_COOLDOWN_FROM = text(
    "SELECT max(created_at), clock_timestamp() FROM automation_control_events "
    f"WHERE job_key = :key AND action = 'run' AND {catalog.run_counts_for_cooldown()}"
)
_RECORD_RUN = text(
    "INSERT INTO automation_control_events (request_id, job_key, action, outcome, actor_sub, created_at) "
    "VALUES (:rid, :key, 'run', 'requested', :actor, clock_timestamp()) "
    "ON CONFLICT (request_id) DO NOTHING RETURNING request_id"
)
# Guarded by outcome = 'requested': the answer is written once, and never over a row
# some other writer has already moved on.
_RECORD_INVOKE = text(
    "UPDATE automation_control_events SET outcome = CAST(:outcome AS text), "
    "invoked_at = CASE WHEN CAST(:outcome AS text) = 'invoked' THEN clock_timestamp() END "
    "WHERE request_id = :rid AND action = 'run' AND outcome = 'requested'"
)
_READ_RUN = text(
    f"SELECT {_RUN_COLUMNS}, "
    "(SELECT max(created_at) FROM automation_control_events c "
    f" WHERE c.job_key = e.job_key AND c.action = 'run' AND {catalog.run_counts_for_cooldown('c')}), "
    "clock_timestamp() "
    "FROM automation_control_events e WHERE request_id = :rid"
)


def _read_run(db: Session, request_id: Any) -> Tuple[RunRow, datetime]:
    r = db.execute(_READ_RUN, {"rid": request_id}).one()
    return RunRow(*r[:8]), r[8]


def _claim_run(db: Session, job: catalog.JobDef, request_id: Any, actor: str) -> Optional[AutomationRunResult]:
    """One short write transaction, taking 3b's locks in 3b's order (control row, then
    the event key). Returns the replayed result for a known request_id, None once a
    new `requested` row is committed; raises RunRejected / ControlConflict."""
    db.execute(text("SET TRANSACTION READ WRITE"))  # a pooler session can leak read-only
    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    db.execute(text("SET LOCAL statement_timeout = '5s'"))
    # Serialises every run-now (and pause/resume) of this job behind one row lock, so
    # two confirmations with different ids cannot both pass the cooldown check.
    paused, *_ = db.execute(_LOCK_CONTROL, {"key": job.key}).one()

    def replay_or_conflict() -> Optional[AutomationRunResult]:
        stored = db.execute(_STORED_EVENT, {"rid": request_id}).first()
        if stored is None:
            return None
        if stored[0] != job.key or stored[1] != "run":
            db.rollback()
            raise ControlConflict()
        row, now = _read_run(db, request_id)
        db.rollback()  # drops the control row this request may have created on first use
        return AutomationRunResult(
            request_id=request_id, job_key=job.key, outcome=row.outcome, replayed=True,
            run=run_state(row, now),
        )

    # A known id replays before any gate: its answer must not change because the job
    # was paused, or its cooldown started, after it was first processed.
    replay = replay_or_conflict()
    if replay is not None:
        return replay
    if paused:
        db.rollback()
        raise RunRejected("job_paused")
    cooldown_from, now = db.execute(_COOLDOWN_FROM, {"key": job.key}).one()
    if cooldown_from is not None:
        until = cooldown_from + timedelta(seconds=catalog.RUN_COOLDOWN_SECONDS)
        if now < until:
            db.rollback()
            raise RunRejected("cooldown", until, max(1, math.ceil((until - now).total_seconds())))
    recorded = db.execute(_RECORD_RUN, {"rid": request_id, "key": job.key, "actor": actor}).first()
    if recorded is None:
        # The id was taken by a concurrent request for ANOTHER job (same-job requests
        # wait on the control row lock above, and would have replayed).
        replay = replay_or_conflict()
        if replay is not None:
            return replay
        raise RuntimeError("request_id vanished between INSERT and SELECT")  # pragma: no cover
    db.commit()
    return None


def _record_invoke(db: Session, request_id: Any, outcome: str) -> Tuple[RunRow, datetime]:
    db.execute(text("SET TRANSACTION READ WRITE"))
    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    db.execute(text("SET LOCAL statement_timeout = '5s'"))
    db.execute(_RECORD_INVOKE, {"rid": request_id, "outcome": outcome})
    row, now = _read_run(db, request_id)
    db.commit()
    return row, now
