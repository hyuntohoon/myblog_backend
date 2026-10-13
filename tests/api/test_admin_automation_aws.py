"""FEAT-automation-admin 2d — the AWS adapter.

No AWS here: the four section readers take stub clients, and the pure parts
(metric plan, folding, liveness, view) are tested directly. What is pinned:

* the catalog derives exactly the rules / alarms / queues the IAM grant names;
* "no evidence ⇒ unknown", and a failing rule never reads as fresh;
* one deadline bounds the whole read, and the read never raises;
* the DB session is closed before any AWS call;
* the response carries names, never an ARN or the account id;
* the production reader builds every client with an explicit botocore Config.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.services import automation_aws as aws_mod
from app.services import automation_catalog as catalog
from app.services.automation_aws import (
    AlarmState,
    AwsRead,
    FunctionMetrics,
    QueueDepth,
    RuleMetrics,
    RuleState,
    Section,
    fold_metrics,
    plan_metrics,
    queue_url,
    read_alarms,
    read_aws_state,
    read_queues,
    read_rules,
    unavailable_read,
)
from app.services.automation_service import AutomationService, _aws_source, assemble

NOW = datetime(2026, 10, 13, 3, 7, 30, tzinfo=timezone.utc)
FAST = "worker-lyrics-incremental"  # rate(15 min): bound 1800 s, has an alarm
WARM = "backend-api-warm-ping"  # rate(5 min): bound 900 s, no alarm
WEEKLY = "worker-isrc-backfill"
ACCOUNT_ID = "338183196042"


def _rule(name):
    return next(r for r in catalog.AWS_RULES if r.name == name)


def _ok(at=NOW):
    return Section(ok=True, observed_at=at)


def _read(*, rules=None, metrics=None, alarms=None, queues=None, functions=None,
          rules_ok=True, metrics_ok=True, alarms_ok=True, queues_ok=True):
    """An AwsRead where every catalogued rule is ENABLED and delivered 60 s ago."""
    default_rules = {r.name: RuleState("ENABLED", "rate(15 minutes)") for r in catalog.AWS_RULES}
    default_metrics = {
        r.name: RuleMetrics(NOW - timedelta(seconds=60), 96, 0, 86400) for r in catalog.AWS_RULES
    }
    default_alarms = {a: AlarmState("OK", NOW - timedelta(days=3)) for a in catalog.AWS_ALARMS}
    bad = Section(ok=False, observed_at=None, detail="boom")
    return AwsRead(
        rules_section=_ok() if rules_ok else bad,
        rules={**default_rules, **(rules or {})} if rules_ok else {},
        metrics_section=_ok() if metrics_ok else bad,
        rule_metrics={**default_metrics, **(metrics or {})} if metrics_ok else {},
        function_metrics=functions if functions is not None else {
            f.name: FunctionMetrics(100, 0, 0) for f in catalog.AWS_FUNCTIONS
        },
        queues_section=_ok() if queues_ok else bad,
        queues=queues if queues is not None else {q.name: QueueDepth(0, 0, 0) for q in catalog.AWS_QUEUES},
        alarms_section=_ok() if alarms_ok else bad,
        alarms={**default_alarms, **(alarms or {})} if alarms_ok else {},
    )


def _job_for(snapshot, rule_name):
    key = _rule(rule_name).job_key
    return next(j for j in snapshot.jobs if j.key == key)


def _snap(aws):
    return assemble(None, fallback_now=NOW, aws=aws)


# ── catalog ↔ infra ─────────────────────────────────────────────────────────────


class TestCatalog:
    def test_every_eventbridge_job_has_a_rule(self):
        crons = [j for j in catalog.JOBS if j.group == "aws_schedule"]
        assert len(crons) == 18
        assert {j.aws_rule for j in crons} == {r.name for r in catalog.AWS_RULES}
        assert len({r.name for r in catalog.AWS_RULES}) == 18

    def test_only_worker_crons_have_a_failed_invocations_alarm(self):
        # infra/monitoring.tf: for_each over worker_cron; warm pings have none.
        with_alarm = {r.name for r in catalog.AWS_RULES if r.alarm}
        assert with_alarm == {r.name for r in catalog.AWS_RULES if r.name.startswith("worker-")}
        assert _rule(FAST).alarm == f"{FAST}-failed-invocations"
        assert _rule(WARM).alarm is None

    def test_alarm_names_match_infra(self):
        # The IAM grant lists these exact 24 alarms (infra/iam_roles.tf).
        assert len(catalog.AWS_ALARMS) == 24 == len(set(catalog.AWS_ALARMS))
        for fixed in ("album-sync-dlq-messages", "worker-cron-dlq-messages",
                      "ratemymusic-api-errors", "blogWorkerLambda-throttles"):
            assert fixed in catalog.AWS_ALARMS

    def test_queues(self):
        assert [q.name for q in catalog.AWS_QUEUES] == ["blogSQS", "album-sync-dlq", "worker-cron-dlq"]

    @pytest.mark.parametrize("name,interval,bound", [
        ("backend-api-warm-ping", 300, 900),  # min 15 min wins
        (FAST, 900, 1800),
        ("worker-release-upcoming-itunes", 1800, 3600),
        ("worker-genius-fetch", 3600, 7200),
        ("worker-album-catalog-ingest", 86400, 172800),
        (WEEKLY, 604800, 1209600),
    ])
    def test_staleness_is_two_intervals_min_15_min(self, name, interval, bound):
        r = _rule(name)
        assert (r.interval_seconds, r.stale_after_seconds) == (interval, bound)


# ── metric plan and folding ─────────────────────────────────────────────────────


class TestMetricPlan:
    def test_windows_are_hour_aligned_and_cover_each_bound(self):
        plan = plan_metrics(NOW)
        (s_start, s_end, short), (l_start, l_end, long) = plan.windows
        assert s_start.minute == l_start.minute == 0 and s_start.second == 0
        assert s_end == l_end and s_end > NOW
        assert s_end - s_start >= timedelta(hours=24)
        assert l_end - l_start >= timedelta(days=14)
        short_rules = {q["MetricStat"]["Metric"]["Dimensions"][0]["Value"] for q in short if q["Id"].startswith("r")}
        long_rules = {q["MetricStat"]["Metric"]["Dimensions"][0]["Value"] for q in long}
        assert short_rules.isdisjoint(long_rules)
        assert short_rules | long_rules == {r.name for r in catalog.AWS_RULES}
        for r in catalog.AWS_RULES:
            window = (l_end - l_start) if r.name in long_rules else (s_end - s_start)
            # The window must reach back past the bound, or "no delivery" would be
            # claimed for a rule whose last delivery is merely outside the window.
            assert window.total_seconds() >= r.stale_after_seconds, r.name

    def test_query_ids_unique_and_within_api_limits(self):
        plan = plan_metrics(NOW)
        ids = [q["Id"] for _, _, qs in plan.windows for q in qs]
        assert len(ids) == len(set(ids))
        assert all(len(qs) <= 500 for _, _, qs in plan.windows)
        assert all(q["MetricStat"]["Stat"] == "Sum" for _, _, qs in plan.windows for q in qs)

    def test_fine_period_for_sub_hourly_rules(self):
        assert aws_mod.rule_period(_rule(WARM)) == 300
        assert aws_mod.rule_period(_rule("worker-genius-fetch")) == 3600


class TestFold:
    def _series(self, rule_name, inv=(), failed=()):
        plan = plan_metrics(NOW)
        inv_id, fail_id = plan.rule_ids[rule_name]
        return plan, {inv_id: list(inv), fail_id: list(failed)}

    def test_last_delivery_is_the_newest_nonzero_period(self):
        plan, series = self._series(FAST, inv=[
            (NOW - timedelta(minutes=10), 1.0),
            (NOW - timedelta(minutes=5), 0.0),  # a zero datapoint is not a delivery
            (NOW - timedelta(minutes=25), 1.0),
        ])
        rules, _ = fold_metrics(plan, series, NOW)
        assert rules[FAST].last_delivery_at == NOW - timedelta(minutes=10)

    def test_24h_counts_exclude_older_points(self):
        plan, series = self._series(FAST, inv=[
            (NOW - timedelta(hours=1), 4.0), (NOW - timedelta(hours=25), 7.0),
        ], failed=[(NOW - timedelta(hours=2), 2.0), (NOW - timedelta(hours=30), 9.0)])
        rules, _ = fold_metrics(plan, series, NOW)
        assert (rules[FAST].delivered_24h, rules[FAST].failed_24h) == (4, 2)

    def test_no_datapoints_means_no_delivery(self):
        rules, functions = fold_metrics(plan_metrics(NOW), {}, NOW)
        assert rules[FAST] == RuleMetrics(None, 0, 0, 86400)
        assert rules[WEEKLY].lookback_seconds == 15 * 86400
        assert functions["blogWorkerLambda"] == FunctionMetrics(0, 0, 0)

    def test_function_metrics(self):
        plan = plan_metrics(NOW)
        inv, err, thr = plan.function_ids["blogWorkerLambda"]
        t = NOW - timedelta(hours=1)
        _, functions = fold_metrics(plan, {inv: [(t, 50.0)], err: [(t, 2.0)], thr: [(t, 1.0)]}, NOW)
        assert functions["blogWorkerLambda"] == FunctionMetrics(50, 2, 1)


# ── liveness: never fresh without evidence, never fresh while failing ───────────


class TestRuleLiveness:
    def test_fresh_within_bound(self):
        live = _job_for(_snap(_read()), FAST).liveness
        assert live.status == "fresh"
        assert live.observed_at == NOW - timedelta(seconds=60)
        assert "96 delivered, 0 failed" in live.reason and "alarm OK" in live.reason

    def test_boundary(self):
        bound = _rule(FAST).stale_after_seconds
        at = _read(metrics={FAST: RuleMetrics(NOW - timedelta(seconds=bound), 1, 0, 86400)})
        assert _job_for(_snap(at), FAST).liveness.status == "fresh"
        over = _read(metrics={FAST: RuleMetrics(NOW - timedelta(seconds=bound + 1), 1, 0, 86400)})
        assert _job_for(_snap(over), FAST).liveness.status == "stale"

    def test_no_delivery_in_window_is_stale_not_unknown(self):
        live = _job_for(_snap(_read(metrics={FAST: RuleMetrics(None, 0, 0, 86400)})), FAST).liveness
        assert live.status == "stale"
        assert live.observed_at is None
        assert "No delivery in the last 24 h" in live.reason

    def test_disabled_rule_is_stale_even_with_recent_deliveries(self):
        live = _job_for(_snap(_read(rules={FAST: RuleState("DISABLED", "rate(15 minutes)")})), FAST).liveness
        assert live.status == "stale"
        assert "DISABLED" in live.reason

    def test_failed_deliveries_are_failing(self):
        live = _job_for(_snap(_read(metrics={FAST: RuleMetrics(NOW - timedelta(seconds=60), 96, 3, 86400)})), FAST).liveness
        assert live.status == "failing"
        assert "3 failed" in live.reason

    def test_alarm_in_alarm_is_failing(self):
        alarm = _rule(FAST).alarm
        live = _job_for(_snap(_read(alarms={alarm: AlarmState("ALARM", NOW)})), FAST).liveness
        assert live.status == "failing"
        assert "alarm ALARM" in live.reason

    def test_unread_alarm_is_said_not_assumed(self):
        live = _job_for(_snap(_read(alarms_ok=False)), FAST).liveness
        assert live.status == "fresh"  # metrics alone still evidence the deliveries
        assert "alarm not read" in live.reason

    def test_rule_without_alarm(self):
        live = _job_for(_snap(_read()), WARM).liveness
        assert live.status == "fresh"
        assert "alarm" not in live.reason

    @pytest.mark.parametrize("kw,missing", [
        ({"rules_ok": False}, "rule state"),
        ({"metrics_ok": False}, "delivery metrics"),
    ])
    def test_missing_read_is_unknown(self, kw, missing):
        live = _job_for(_snap(_read(**kw)), FAST).liveness
        assert live.status == "unknown"
        assert missing in live.reason

    def test_a_rule_the_read_did_not_return_is_unknown(self):
        read = _read()
        del read.rules[FAST]
        assert _job_for(_snap(read), FAST).liveness.status == "unknown"

    def test_no_reader_keeps_every_aws_row_unknown(self):
        snap = _snap(None)
        assert snap.aws is None
        assert all(j.liveness.status == "unknown" for j in snap.jobs if j.location == "aws")
        assert {s.key: s.status for s in snap.sources}["aws"] == "not_wired"

    def test_scheduler_and_message_rows_stay_unknown(self):
        snap = _snap(_read())
        for j in snap.jobs:
            if j.group in ("aws_scheduler", "aws_queue_message"):
                assert j.liveness.status == "unknown", j.key


# ── the aws block and the source line ───────────────────────────────────────────


class TestAwsView:
    def test_items_carry_status_and_observed_at(self):
        view = _snap(_read()).aws
        assert len(view.rules) == 18 and len(view.alarms) == 24
        assert len(view.queues) == 3 and len(view.functions) == 3
        for item in [*view.rules, *view.queues, *view.functions, *view.alarms]:
            assert item.status == "ok" and item.observed_at is not None

    def test_failed_section_gives_null_numbers_not_zero(self):
        view = _snap(_read(queues_ok=False, metrics_ok=False)).aws
        for q in view.queues:
            assert (q.status, q.observed_at, q.visible, q.in_flight, q.delayed) == ("unknown", None, None, None, None)
        for f in view.functions:
            assert (f.status, f.errors_24h, f.invocations_24h) == ("unknown", None, None)
        for r in view.rules:
            assert r.status == "unknown"
            assert r.state == "ENABLED"  # the half that was read is still shown
            assert r.delivered_24h is None and r.last_delivery_at is None

    def test_partially_read_queue_list(self):
        view = _snap(_read(queues={"blogSQS": QueueDepth(3, 1, 0)})).aws
        by = {q.name: q for q in view.queues}
        assert (by["blogSQS"].status, by["blogSQS"].visible, by["blogSQS"].in_flight) == ("ok", 3, 1)
        assert by["album-sync-dlq"].status == "unknown" and by["album-sync-dlq"].visible is None

    def test_source_line(self):
        assert _aws_source(_read()).status == "ok"
        partial = _aws_source(_read(alarms_ok=False))
        assert partial.status == "ok" and "alarms: boom" in partial.detail
        dead = _aws_source(unavailable_read("AccessDenied"))
        assert dead.status == "unavailable" and dead.observed_at is None

    def test_access_denied_everywhere_renders_unknown_not_empty(self):
        snap = _snap(unavailable_read("GetMetricData failed (AccessDenied)."))
        assert all(j.liveness.status == "unknown" for j in snap.jobs if j.location == "aws")
        assert all(q.visible is None for q in snap.aws.queues)
        assert len(snap.aws.rules) == 18  # every row is still listed

    def test_no_arn_or_account_id_in_the_response(self):
        body = _snap(_read()).model_dump_json()
        assert "arn:" not in body
        assert ACCOUNT_ID not in body
        assert "sqs.ap-northeast-2.amazonaws.com" not in body


# ── section readers against stub clients ────────────────────────────────────────


class _Err(Exception):
    def __init__(self, code):
        super().__init__(f"An error occurred ({code}) on arn:aws:events:ap-northeast-2:{ACCOUNT_ID}:rule/x")
        self.response = {"Error": {"Code": code}}


class TestReaders:
    def test_rules_partial_failure(self):
        class Events:
            def describe_rule(self, Name):
                if Name == FAST:
                    raise _Err("AccessDeniedException")
                return {"State": "ENABLED", "ScheduleExpression": "rate(5 minutes)", "Arn": "arn:..."}
        section, rules = read_rules(Events(), lambda: NOW)
        assert section.ok and FAST not in rules and len(rules) == 17
        assert "1 of 18 rules not read (AccessDeniedException)" in section.detail
        assert ACCOUNT_ID not in section.detail  # code only, never the message

    def test_rules_all_denied(self):
        class Events:
            def describe_rule(self, Name):
                raise _Err("AccessDeniedException")
        section, rules = read_rules(Events(), lambda: NOW)
        assert not section.ok and rules == {}
        assert section.detail == "DescribeRule failed (AccessDeniedException)."

    def test_queue_url_is_a_sibling_of_the_configured_one(self):
        base = f"https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/blogSQS"
        assert queue_url(base, "worker-cron-dlq") == f"https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT_ID}/worker-cron-dlq"
        assert queue_url("", "x") is None

    def test_queues_read_only_attributes(self):
        calls = []

        class Sqs:
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                calls.append((QueueUrl.rsplit("/", 1)[1], tuple(AttributeNames)))
                return {"Attributes": {"ApproximateNumberOfMessages": "2",
                                       "ApproximateNumberOfMessagesNotVisible": "1",
                                       "ApproximateNumberOfMessagesDelayed": "0"}}
        section, queues = read_queues(Sqs(), "https://sqs.x/1/blogSQS", lambda: NOW)
        assert section.ok and queues["blogSQS"] == QueueDepth(2, 1, 0)
        assert [c[0] for c in calls] == ["blogSQS", "album-sync-dlq", "worker-cron-dlq"]
        assert all(c[1] == aws_mod.QUEUE_ATTRIBUTES for c in calls)

    def test_queues_without_url(self):
        section, queues = read_queues(object(), "", lambda: NOW)
        assert not section.ok and queues == {} and "SQS_QUEUE_URL" in section.detail

    def test_alarms_asks_only_for_catalogued_names(self):
        seen = {}

        class Cw:
            def describe_alarms(self, **kw):
                seen.update(kw)
                return {"MetricAlarms": [
                    {"AlarmName": "blogWorkerLambda-errors", "StateValue": "ALARM", "StateUpdatedTimestamp": NOW},
                    {"AlarmName": "somebody-elses-alarm", "StateValue": "OK"},
                ]}
        section, alarms = read_alarms(Cw(), lambda: NOW)
        assert seen["AlarmNames"] == list(catalog.AWS_ALARMS) and len(seen["AlarmNames"]) <= 100
        assert set(alarms) == {"blogWorkerLambda-errors"}
        assert "23 catalogued alarm(s) not found." == section.detail

    def test_metrics_failure_is_unavailable(self):
        class Cw:
            def get_metric_data(self, **kw):
                raise _Err("AccessDenied")
        section, rules, functions = aws_mod.read_metrics(Cw(), lambda: NOW)
        assert not section.ok and rules == {} and functions == {}
        assert section.detail == "GetMetricData failed (AccessDenied)."

    def test_an_incomplete_metric_result_is_not_read_as_no_deliveries(self):
        # A result with StatusCode InternalError carries no datapoints; folding it
        # would claim "no delivery" (stale) from a read that did not happen.
        class Cw:
            def get_metric_data(self, **kw):
                return {"MetricDataResults": [{"Id": "r0i", "StatusCode": "InternalError", "Timestamps": [], "Values": []}]}
        section, rules, _ = aws_mod.read_metrics(Cw(), lambda: NOW)
        assert not section.ok and rules == {}

    def test_metrics_reads_both_windows_and_pages(self):
        calls = []

        class Cw:
            def get_metric_data(self, **kw):
                calls.append(kw)
                if "NextToken" not in kw and len(calls) == 1:
                    return {"MetricDataResults": [], "NextToken": "t"}
                return {"MetricDataResults": []}
        section, rules, _ = aws_mod.read_metrics(Cw(), lambda: NOW)
        assert section.ok and len(rules) == 18
        assert len(calls) == 3 and calls[1]["NextToken"] == "t"


class _Clients:
    """Stub clients for read_aws_state. `hang` blocks one section."""

    def __init__(self, hang=None, release=None):
        self.hang, self.release = hang, release

    def _maybe_hang(self, name):
        if self.hang == name:
            self.release.wait(5)

    def describe_rule(self, Name):
        self._maybe_hang("rules")
        return {"State": "ENABLED"}

    def get_metric_data(self, **kw):
        self._maybe_hang("metrics")
        return {"MetricDataResults": []}

    def describe_alarms(self, **kw):
        self._maybe_hang("alarms")
        return {"MetricAlarms": []}

    def get_queue_attributes(self, **kw):
        self._maybe_hang("queues")
        return {"Attributes": {"ApproximateNumberOfMessages": "0",
                               "ApproximateNumberOfMessagesNotVisible": "0",
                               "ApproximateNumberOfMessagesDelayed": "0"}}


class TestOrchestration:
    def test_all_sections(self):
        c = _Clients()
        read = read_aws_state(events=c, cloudwatch=c, sqs=c, sqs_base_url="https://sqs.x/1/blogSQS", now=lambda: NOW)
        assert all(s.ok for s in (read.rules_section, read.metrics_section, read.queues_section, read.alarms_section))

    def test_deadline_bounds_a_hanging_section(self):
        release = threading.Event()
        c = _Clients(hang="metrics", release=release)
        started = time.monotonic()
        try:
            read = read_aws_state(events=c, cloudwatch=c, sqs=c, sqs_base_url="https://sqs.x/1/blogSQS",
                                  now=lambda: NOW, deadline_seconds=0.3)
        finally:
            release.set()
        assert time.monotonic() - started < 2
        assert not read.metrics_section.ok and "Timed out" in read.metrics_section.detail
        assert read.rule_metrics == {} and read.function_metrics == {}
        assert read.rules_section.ok and read.queues_section.ok and read.alarms_section.ok

    def test_a_raising_section_never_escapes(self):
        c = _Clients()
        with patch.object(aws_mod, "read_alarms", side_effect=RuntimeError("x")):
            read = read_aws_state(events=c, cloudwatch=c, sqs=c, sqs_base_url="u/blogSQS", now=lambda: NOW)
        assert not read.alarms_section.ok and read.alarms_section.detail == "Read failed (RuntimeError)."


# ── service order and client construction ───────────────────────────────────────


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._rows[0][0]


class _Session:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, stmt):
        if " ".join(str(stmt).split()) == "SELECT now()":
            return _Result([(NOW,)])
        return _Result([])


def test_aws_is_read_after_the_db_session_closes():
    session = _Session()
    seen = []

    def reader():
        seen.append(session.closed)
        return _read()

    snap = AutomationService(lambda: session, aws_reader=reader).snapshot()
    assert seen == [True]
    assert {s.key: s.status for s in snap.sources}["aws"] == "ok"
    assert _job_for(snap, FAST).liveness.status == "fresh"


def test_production_reader_uses_an_explicit_config():
    configs = {}

    def client(service, region_name, config):
        configs[service] = (region_name, config)
        return _Clients()

    with patch("boto3.client", side_effect=client):
        read = aws_mod.build_reader("ap-northeast-2", "https://sqs.x/1/blogSQS")()
    assert set(configs) == {"events", "cloudwatch", "sqs"}
    for region, cfg in configs.values():
        assert region == "ap-northeast-2"
        assert cfg.connect_timeout == aws_mod.CONNECT_TIMEOUT_SECONDS
        assert cfg.read_timeout == aws_mod.READ_TIMEOUT_SECONDS
        assert cfg.retries == {"max_attempts": aws_mod.MAX_ATTEMPTS, "mode": "standard"}
    assert read.rules_section.ok


def test_client_setup_failure_is_unavailable_not_raised():
    with patch("boto3.client", side_effect=RuntimeError("no creds")):
        read = aws_mod.build_reader("ap-northeast-2", "")()
    assert not read.rules_section.ok and "RuntimeError" in read.rules_section.detail


def test_deadline_fits_the_lambda_timeout():
    # Backend Lambda timeout is 15 s (infra/lambda.tf) and the DB read comes first.
    assert aws_mod.DEADLINE_SECONDS <= 8
    assert aws_mod.CONNECT_TIMEOUT_SECONDS + aws_mod.READ_TIMEOUT_SECONDS <= aws_mod.DEADLINE_SECONDS
