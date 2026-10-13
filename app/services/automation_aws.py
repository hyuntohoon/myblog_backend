"""AWS adapter for the owner's automation page (FEAT-automation-admin 2d).

Read-only. Four independent sections, each with its own status and
`observed_at`, so one failing read never blanks the others:

* rules   — `events:DescribeRule` per catalogued rule (state, schedule);
* metrics — `cloudwatch:GetMetricData`, batched: per-rule Invocations /
            FailedInvocations and per-function Errors / Throttles / Invocations;
* queues  — `sqs:GetQueueAttributes` for blogSQS and its two dead-letter queues;
* alarms  — `cloudwatch:DescribeAlarms` for the alarms `infra/monitoring.tf` declares.

The IAM grant (`infra/iam_roles.tf` `backend_automation_read`) is scoped to
exactly these rules, queues and alarms. Nothing here writes, invokes, enables
or disables anything; opening the page starts nothing.

Every call carries an explicit botocore `Config` (connect/read timeout, bounded
retries), and the whole read runs under one deadline: the backend Lambda's
timeout is 15 s and the DB read comes first. A section that misses the deadline
is `unavailable`, never empty. The caller must have closed its DB session before
calling `read_aws_state` (fetch DB → close → AWS → assemble).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services import automation_catalog as catalog

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 3
MAX_ATTEMPTS = 2  # botocore counts the first try: one retry at most
DEADLINE_SECONDS = 6.0

# Metric windows. A rule whose staleness bound fits in a day is read at fine
# resolution over the last day; the daily and weekly rules over the last
# `LONG_WINDOW_DAYS` at hourly resolution (CloudWatch keeps 1-hour data for
# 455 days, 5-minute data for 63).
DAY_SECONDS = 24 * 60 * 60
LONG_WINDOW_DAYS = 15
FINE_PERIOD = 300
HOURLY_PERIOD = 3600

QUEUE_ATTRIBUTES = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)


@dataclass(frozen=True)
class Section:
    ok: bool
    observed_at: Optional[datetime]
    detail: Optional[str] = None


@dataclass(frozen=True)
class RuleState:
    state: Optional[str]  # ENABLED / DISABLED / ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS
    schedule: Optional[str]


@dataclass(frozen=True)
class RuleMetrics:
    # Start of the newest period with a SUCCESSFUL delivery (Invocations minus
    # FailedInvocations > 0 — EventBridge counts a failed delivery as an
    # invocation too). The delivery was inside [last_delivery_at, + period), so
    # an age measured from here is never younger than the real one.
    last_delivery_at: Optional[datetime]
    delivered_24h: int
    failed_24h: int
    lookback_seconds: int
    # The newest period with any delivery attempt had failures. Kept apart from
    # failed_24h: a weekly rule's last attempt can be days old, and the
    # FailedInvocations alarm returns to OK 5 minutes after it fires.
    last_attempt_failed: bool = False
    failed_in_lookback: int = 0
    last_failure_at: Optional[datetime] = None  # start of the newest period with a failure


@dataclass(frozen=True)
class FunctionMetrics:
    invocations_24h: int
    errors_24h: int
    throttles_24h: int


@dataclass(frozen=True)
class QueueDepth:
    visible: int
    in_flight: int
    delayed: int


@dataclass(frozen=True)
class AlarmState:
    state: str
    updated_at: Optional[datetime]


@dataclass(frozen=True)
class AwsRead:
    rules_section: Section
    rules: Dict[str, RuleState]  # rule name → state; a rule missing here was not read
    metrics_section: Section
    rule_metrics: Dict[str, RuleMetrics]
    function_metrics: Dict[str, FunctionMetrics]
    queues_section: Section
    queues: Dict[str, QueueDepth]
    alarms_section: Section
    alarms: Dict[str, AlarmState]


def _unavailable(detail: str) -> Section:
    return Section(ok=False, observed_at=None, detail=detail)


def unavailable_read(detail: str) -> AwsRead:
    s = _unavailable(detail)
    return AwsRead(s, {}, s, {}, {}, s, {}, s, {})


def _error_code(exc: BaseException) -> str:
    """The AWS error code or the exception type — never the message (it can carry ARNs)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


# ── rules ──────────────────────────────────────────────────────────────────────


def read_rules(events: Any, now: Callable[[], datetime]) -> Tuple[Section, Dict[str, RuleState]]:
    out: Dict[str, RuleState] = {}
    failed: Dict[str, str] = {}
    for rule in catalog.AWS_RULES:
        try:
            r = events.describe_rule(Name=rule.name)
        except Exception as exc:  # noqa: BLE001 — one rule's failure must not hide the others
            failed[rule.name] = _error_code(exc)
            continue
        out[rule.name] = RuleState(state=r.get("State"), schedule=r.get("ScheduleExpression"))
    if not out:
        codes = sorted(set(failed.values()))
        return _unavailable(f"DescribeRule failed ({', '.join(codes) or 'no rules'})."), out
    detail = None
    if failed:
        detail = f"{len(failed)} of {len(catalog.AWS_RULES)} rules not read ({', '.join(sorted(set(failed.values())))})."
    return Section(ok=True, observed_at=now(), detail=detail), out


# ── metrics ────────────────────────────────────────────────────────────────────


def rule_period(rule: catalog.AwsRule) -> int:
    # Up to hourly rules use 5-minute periods: an hourly period would date a
    # :55 delivery to :00 and make the rule flicker stale late in each hour.
    return FINE_PERIOD if rule.interval_seconds <= HOURLY_PERIOD else HOURLY_PERIOD


def rule_is_long(rule: catalog.AwsRule) -> bool:
    """True when the staleness bound needs more than the one-day window."""
    return rule.stale_after_seconds + rule_period(rule) > DAY_SECONDS


def _query(qid: str, namespace: str, metric: str, dim_name: str, dim_value: str, period: int) -> Dict[str, Any]:
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {"Namespace": namespace, "MetricName": metric,
                       "Dimensions": [{"Name": dim_name, "Value": dim_value}]},
            "Period": period,
            "Stat": "Sum",
        },
        "ReturnData": True,
    }


@dataclass(frozen=True)
class MetricPlan:
    """The GetMetricData batches, with each query id mapped back to its subject."""

    windows: Tuple[Tuple[datetime, datetime, Tuple[Dict[str, Any], ...]], ...]
    rule_ids: Dict[str, Tuple[str, str]]  # rule name → (invocations id, failed id)
    function_ids: Dict[str, Tuple[str, str, str]]  # function → (invocations, errors, throttles)


def plan_metrics(now: datetime) -> MetricPlan:
    """Pure: two windows aligned to hour boundaries (CloudWatch's own advice for
    GetMetricData), so every period, fine or hourly, starts on a boundary."""
    end = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    short_start = (end - timedelta(seconds=DAY_SECONDS)).replace(minute=0)
    long_start = (end - timedelta(days=LONG_WINDOW_DAYS)).replace(minute=0)

    short: List[Dict[str, Any]] = []
    long: List[Dict[str, Any]] = []
    rule_ids: Dict[str, Tuple[str, str]] = {}
    for i, rule in enumerate(catalog.AWS_RULES):
        inv, fail = f"r{i}i", f"r{i}f"
        rule_ids[rule.name] = (inv, fail)
        target = long if rule_is_long(rule) else short
        p = rule_period(rule)
        target.append(_query(inv, "AWS/Events", "Invocations", "RuleName", rule.name, p))
        target.append(_query(fail, "AWS/Events", "FailedInvocations", "RuleName", rule.name, p))

    function_ids: Dict[str, Tuple[str, str, str]] = {}
    for i, fn in enumerate(catalog.AWS_FUNCTIONS):
        ids = (f"f{i}i", f"f{i}e", f"f{i}t")
        function_ids[fn.name] = ids
        for qid, metric in zip(ids, ("Invocations", "Errors", "Throttles")):
            short.append(_query(qid, "AWS/Lambda", metric, "FunctionName", fn.name, HOURLY_PERIOD))

    windows = tuple(
        (start, end, tuple(queries))
        for start, queries in ((short_start, short), (long_start, long))
        if queries
    )
    return MetricPlan(windows=windows, rule_ids=rule_ids, function_ids=function_ids)


def _fetch_series(cloudwatch: Any, start: datetime, end: datetime, queries: Tuple[Dict[str, Any], ...]) -> Dict[str, List[Tuple[datetime, float]]]:
    series: Dict[str, List[Tuple[datetime, float]]] = {}
    kwargs: Dict[str, Any] = {"MetricDataQueries": list(queries), "StartTime": start, "EndTime": end}
    for _ in range(5):  # a bounded number of pages; a normal read is one
        resp = cloudwatch.get_metric_data(**kwargs)
        for result in resp.get("MetricDataResults", []):
            if result.get("StatusCode") not in (None, "Complete", "PartialData"):
                raise RuntimeError(f"metric {result.get('Id')} status {result.get('StatusCode')}")
            series.setdefault(result["Id"], []).extend(zip(result.get("Timestamps", []), result.get("Values", [])))
        token = resp.get("NextToken")
        if not token:
            return series
        kwargs["NextToken"] = token
    raise RuntimeError("GetMetricData pagination did not finish")


def _sum_since(points: List[Tuple[datetime, float]], since: datetime) -> int:
    return int(sum(v for ts, v in points if ts >= since))


def fold_metrics(plan: MetricPlan, series: Dict[str, List[Tuple[datetime, float]]], now: datetime) -> Tuple[Dict[str, RuleMetrics], Dict[str, FunctionMetrics]]:
    """Pure. An id with no datapoints means no deliveries: CloudWatch records
    every one, so an empty series is evidence, not a missing read."""
    day_ago = now - timedelta(seconds=DAY_SECONDS)
    rules: Dict[str, RuleMetrics] = {}
    for rule in catalog.AWS_RULES:
        inv_id, fail_id = plan.rule_ids[rule.name]
        inv = series.get(inv_id, [])
        failed = series.get(fail_id, [])
        attempts: Dict[datetime, float] = {}
        failures: Dict[datetime, float] = {}
        for ts, v in inv:
            attempts[ts] = attempts.get(ts, 0.0) + v
        for ts, v in failed:
            failures[ts] = failures.get(ts, 0.0) + v
        succeeded = [ts for ts in attempts if attempts[ts] - failures.get(ts, 0.0) > 0]
        tried = [ts for ts in set(attempts) | set(failures) if attempts.get(ts, 0.0) > 0 or failures.get(ts, 0.0) > 0]
        newest = max(tried) if tried else None
        rules[rule.name] = RuleMetrics(
            last_delivery_at=max(succeeded) if succeeded else None,
            delivered_24h=_sum_since(inv, day_ago),
            failed_24h=_sum_since(failed, day_ago),
            lookback_seconds=(LONG_WINDOW_DAYS * DAY_SECONDS) if rule_is_long(rule) else DAY_SECONDS,
            last_attempt_failed=newest is not None and failures.get(newest, 0.0) > 0,
            failed_in_lookback=int(sum(failures.values())),
            last_failure_at=max((ts for ts, v in failures.items() if v > 0), default=None),
        )
    functions: Dict[str, FunctionMetrics] = {}
    for fn in catalog.AWS_FUNCTIONS:
        inv_id, err_id, thr_id = plan.function_ids[fn.name]
        functions[fn.name] = FunctionMetrics(
            invocations_24h=_sum_since(series.get(inv_id, []), day_ago),
            errors_24h=_sum_since(series.get(err_id, []), day_ago),
            throttles_24h=_sum_since(series.get(thr_id, []), day_ago),
        )
    return rules, functions


def read_metrics(cloudwatch: Any, now: Callable[[], datetime]) -> Tuple[Section, Dict[str, RuleMetrics], Dict[str, FunctionMetrics]]:
    t = now()
    plan = plan_metrics(t)
    try:
        series: Dict[str, List[Tuple[datetime, float]]] = {}
        for start, end, queries in plan.windows:
            series.update(_fetch_series(cloudwatch, start, end, queries))
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"GetMetricData failed ({_error_code(exc)})."), {}, {}
    rules, functions = fold_metrics(plan, series, t)
    return Section(ok=True, observed_at=now()), rules, functions


# ── queues ─────────────────────────────────────────────────────────────────────


def queue_url(base_url: str, name: str) -> Optional[str]:
    """Sibling queue URL from the configured blogSQS URL (same account and region)."""
    if not base_url or "/" not in base_url.rstrip("/"):
        return None
    return base_url.rstrip("/").rsplit("/", 1)[0] + "/" + name


def read_queues(sqs: Any, base_url: str, now: Callable[[], datetime]) -> Tuple[Section, Dict[str, QueueDepth]]:
    if not queue_url(base_url, "x"):
        return _unavailable("SQS_QUEUE_URL is not set."), {}
    out: Dict[str, QueueDepth] = {}
    failed: List[str] = []
    for q in catalog.AWS_QUEUES:
        try:
            attrs = sqs.get_queue_attributes(QueueUrl=queue_url(base_url, q.name), AttributeNames=list(QUEUE_ATTRIBUTES))["Attributes"]
            out[q.name] = QueueDepth(
                visible=int(attrs["ApproximateNumberOfMessages"]),
                in_flight=int(attrs["ApproximateNumberOfMessagesNotVisible"]),
                delayed=int(attrs["ApproximateNumberOfMessagesDelayed"]),
            )
        except Exception as exc:  # noqa: BLE001
            failed.append(_error_code(exc))
    if not out:
        return _unavailable(f"GetQueueAttributes failed ({', '.join(sorted(set(failed)))})."), out
    detail = f"{len(failed)} of {len(catalog.AWS_QUEUES)} queues not read." if failed else None
    return Section(ok=True, observed_at=now(), detail=detail), out


# ── alarms ─────────────────────────────────────────────────────────────────────


def read_alarms(cloudwatch: Any, now: Callable[[], datetime]) -> Tuple[Section, Dict[str, AlarmState]]:
    try:
        resp = cloudwatch.describe_alarms(AlarmNames=list(catalog.AWS_ALARMS), AlarmTypes=["MetricAlarm"], MaxRecords=100)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"DescribeAlarms failed ({_error_code(exc)})."), {}
    out = {
        a["AlarmName"]: AlarmState(state=str(a.get("StateValue")), updated_at=a.get("StateUpdatedTimestamp"))
        for a in resp.get("MetricAlarms", [])
        if a.get("AlarmName") in catalog.AWS_ALARMS
    }
    missing = len(catalog.AWS_ALARMS) - len(out)
    detail = f"{missing} catalogued alarm(s) not found." if missing else None
    return Section(ok=True, observed_at=now(), detail=detail), out


# ── orchestration ──────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def read_aws_state(
    *,
    events: Any,
    cloudwatch: Any,
    sqs: Any,
    sqs_base_url: str,
    now: Callable[[], datetime] = _utcnow,
    deadline_seconds: float = DEADLINE_SECONDS,
) -> AwsRead:
    """Run the four sections concurrently under one deadline. Never raises."""
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="automation-aws")
    try:
        futures = {
            "rules": pool.submit(read_rules, events, now),
            "metrics": pool.submit(read_metrics, cloudwatch, now),
            "queues": pool.submit(read_queues, sqs, sqs_base_url, now),
            "alarms": pool.submit(read_alarms, cloudwatch, now),
        }
        started = time.monotonic()
        wait(futures.values(), timeout=deadline_seconds)
        elapsed = time.monotonic() - started

        def result(key: str, empty: Tuple[Any, ...]) -> Tuple[Any, ...]:
            f = futures[key]
            if not f.done():
                logger.warning("automation aws: %s missed the %.1fs deadline", key, deadline_seconds)
                return (_unavailable(f"Timed out after {deadline_seconds:g}s."), *empty)
            exc = f.exception()
            if exc is not None:
                logger.warning("automation aws: %s failed (%s)", key, _error_code(exc))
                return (_unavailable(f"Read failed ({_error_code(exc)})."), *empty)
            return f.result()

        rules_section, rules = result("rules", ({},))
        metrics_section, rule_metrics, function_metrics = result("metrics", ({}, {}))
        queues_section, queues = result("queues", ({},))
        alarms_section, alarms = result("alarms", ({},))
        logger.info("automation aws: read in %.2fs", elapsed)
        return AwsRead(
            rules_section, rules, metrics_section, rule_metrics, function_metrics,
            queues_section, queues, alarms_section, alarms,
        )
    finally:
        # Do not wait for a straggler: its own botocore timeouts end it.
        pool.shutdown(wait=False, cancel_futures=True)


def build_reader(region: str, sqs_base_url: str) -> Callable[[], AwsRead]:
    """The production reader: real boto3 clients with an explicit Config."""

    def reader() -> AwsRead:
        try:
            import boto3
            from botocore.config import Config

            config = Config(
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=READ_TIMEOUT_SECONDS,
                retries={"max_attempts": MAX_ATTEMPTS, "mode": "standard"},
            )
            events = boto3.client("events", region_name=region, config=config)
            cloudwatch = boto3.client("cloudwatch", region_name=region, config=config)
            sqs = boto3.client("sqs", region_name=region, config=config)
        except Exception as exc:  # noqa: BLE001 — no credentials / no boto3 locally
            logger.warning("automation aws: client setup failed (%s)", _error_code(exc))
            return unavailable_read(f"AWS client setup failed ({_error_code(exc)}).")
        return read_aws_state(events=events, cloudwatch=cloudwatch, sqs=sqs, sqs_base_url=sqs_base_url)

    return reader
