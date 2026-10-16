"""Owner run-now invoke (FEAT-automation-admin 3c).

One asynchronous `lambda:InvokeFunction` on `blogWorkerLambda` — the only
function the backend role may invoke (infra/iam_roles.tf) — with the cron's own
constant input from `myblog_shared_db.automation.run_payload`. No caller input
reaches the payload except the request_id, which the route has already parsed as
a UUID.

* `InvocationType=Event`: the request never waits on a worker run. Lambda's async
  config (`worker_async`: 2 retries, then worker-cron-dlq) applies, as for a cron.
* No botocore retry. A retried Event call whose first attempt was accepted would
  start two runs; a failure here is reported as `invoke_failed` instead, which
  starts no cooldown, so the owner can simply press again.
* Explicit connect/read timeouts (CLAUDE.md: every outbound request has one).
* Under ENV=local|dev the invoker is OFF and every run is `invoke_failed`: a local
  backend holding the owner's AWS credentials must never start a production
  worker run against a database it is not looking at.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional
from uuid import UUID

from myblog_shared_db.automation import run_payload

from app.services.automation_catalog import RUN_FUNCTION_NAME

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 5

# (job_key, request_id) -> True when Lambda accepted the async invoke.
Invoker = Callable[[str, UUID], bool]


def _error_code(exc: Exception) -> str:
    # The AWS error code or the exception type — never the message, which can
    # carry the account id and ARNs.
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def invoke_with(client: Any) -> Invoker:
    """An invoker over a boto3 Lambda client (a stub in tests)."""

    def invoke(job_key: str, request_id: UUID) -> bool:
        payload = json.dumps(run_payload(job_key, str(request_id))).encode()
        try:
            response = client.invoke(
                FunctionName=RUN_FUNCTION_NAME, InvocationType="Event", Payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — reported as invoke_failed, never raised
            logger.error("automation run: invoke of %s failed (%s)", job_key, _error_code(exc))
            return False
        status = response.get("StatusCode")
        if status != 202:
            logger.error("automation run: invoke of %s answered %s, not 202", job_key, status)
            return False
        return True

    return invoke


def _disabled(job_key: str, request_id: UUID) -> bool:
    logger.warning("automation run: invoke is disabled under ENV=local|dev; %s not started", job_key)
    return False


def build_invoker(env: str, region: str) -> Invoker:
    """The production invoker, or the disabled one locally."""
    if env in ("local", "dev"):
        return _disabled
    client: Optional[Any] = None

    def invoke(job_key: str, request_id: UUID) -> bool:
        nonlocal client
        if client is None:
            try:
                import boto3
                from botocore.config import Config

                client = boto3.client(
                    "lambda",
                    region_name=region,
                    config=Config(
                        connect_timeout=CONNECT_TIMEOUT_SECONDS,
                        read_timeout=READ_TIMEOUT_SECONDS,
                        retries={"max_attempts": 1, "mode": "standard"},
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — no credentials / no boto3
                logger.error("automation run: client setup failed (%s)", _error_code(exc))
                return False
        return invoke_with(client)(job_key, request_id)

    return invoke
