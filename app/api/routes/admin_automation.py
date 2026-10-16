# app/api/routes/admin_automation.py
# FEAT-automation-admin 2b — the owner's read-only automation snapshot.
#
# Route posture (decided in 2b's security-review; see the RFC design notes):
#   - GET /api/admin/automation                    owner (require_owner, in-Lambda)
#   - GET /api/admin/automation/queue-detail?key=  owner (require_owner, in-Lambda) — 2f
#   - POST /api/admin/automation/controls          owner (JWT route in infra/apigateway.tf
#                                                   + require_owner in-Lambda) — 3b
#   - POST /api/admin/automation/runs              owner (JWT route in infra/apigateway.tf
#                                                   + require_owner in-Lambda) — 3c
#
# It rides the edge_guard GET catch-all with no infra/apigateway.tf route, as
# GET /api/todays-pick/queue does: through CloudFront a missing token is 401
# from the Cognito guard and a non-owner token is 403; on the raw API Gateway
# host edge_guard answers 403. Fail-closed either way.
#
# The GETs ride the catch-all; the 3b and 3c mutations do not (there is no POST
# catch-all), so each has its own Cognito-JWT route — the 2b route decision's
# "revisit when Step 3 adds controls" trigger.
#
# 3c's run-now is the one route here that starts work: an async invoke of
# blogWorkerLambda with a cron's own constant input (never caller input), made
# after its DB transaction has closed.
#
# Opening this starts nothing: one read-only DB transaction, then (snapshot
# only, after the session closes) read-only AWS describes/metrics (2d) — no
# enqueue, no invoke. The response carries AWS resource names and counts, never
# an ARN or the account id.
from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.schemas import (
    AutomationControlRequest,
    AutomationControlResult,
    AutomationQueueDetail,
    AutomationRunRejection,
    AutomationRunRejectionBody,
    AutomationRunRequest,
    AutomationRunResult,
    AutomationSnapshot,
)
from app.core.authz import require_owner, resolve_owner
from app.di import get_automation_service
from app.services.automation_service import AutomationService, ControlConflict, RunRejected

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=AutomationSnapshot)
def get_automation_snapshot(
    _claims: Dict = Depends(require_owner),
    svc: AutomationService = Depends(get_automation_service),
):
    """Every catalogued job with its evidence; unwired sources render unknown."""
    return svc.snapshot()


@router.get("/queue-detail", response_model=AutomationQueueDetail)
def get_automation_queue_detail(
    key: str = Query(..., min_length=1, max_length=256, description="A queue key from the snapshot"),
    _claims: Dict = Depends(require_owner),
    svc: AutomationService = Depends(get_automation_service),
):
    """Why one queue's rows wait, and whether it drains (FEAT-automation-admin 2f).

    Reason codes only — never stored error text. Same read-only transaction
    shape as the snapshot; opening it starts nothing.
    """
    detail = svc.queue_detail(key)
    if detail is None:
        raise HTTPException(status_code=404, detail="Unknown queue")
    return detail


@router.post(
    "/controls",
    response_model=AutomationControlResult,
    responses={
        403: {"description": "Not the owner (answered before any body handling)"},
        404: {"description": "Unknown control: the job has no pause control"},
        409: {"description": "request_id already used for another job or action"},
        503: {"description": "Database unavailable; resend with the SAME request_id"},
    },
)
def post_automation_control(
    body: AutomationControlRequest,
    claims: Dict = Depends(require_owner),
    svc: AutomationService = Depends(get_automation_service),
):
    """Pause or resume one job (FEAT-automation-admin 3b).

    Idempotent twice over: a resent `request_id` replays the stored result, and
    pausing a paused job is `no_change`. Audited in `automation_control_events`.
    Executors honour it at their next boundary; nothing in flight is cancelled.
    """
    try:
        # resolve_owner: the verified `sub`, or the single-owner sentinel under
        # ENV=local|dev where require_owner returns no claims.
        result = svc.control(body.job_key, body.action, body.request_id, resolve_owner(claims))
    except ControlConflict:
        raise HTTPException(status_code=409, detail="request_id already used for another action")
    except SQLAlchemyError as error:
        # Type only, like the executors: the statement and its parameters stay out of logs.
        logger.error("automation control: DB write failed (%s)", type(error).__name__)
        # The commit's outcome can be unknown (an acknowledgement lost after COMMIT), so
        # this never claims "nothing changed". Resending the same request_id is safe:
        # it applies once or replays the stored result.
        raise HTTPException(status_code=503, detail="Control unavailable; resend with the same request_id")
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown control")
    return result


def _rejection(rejection: AutomationRunRejection, headers: Dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": rejection.model_dump(mode="json")}, headers=headers,
    )


@router.post(
    "/runs",
    response_model=AutomationRunResult,
    responses={
        403: {"description": "Not the owner (answered before any body handling)"},
        404: {"description": "Unknown job: it has no run control"},
        409: {
            "model": AutomationRunRejectionBody,
            "description": "job_paused, cooldown (with Retry-After), or request_id_conflict",
        },
        503: {"description": "Database unavailable; resend with the SAME request_id"},
    },
)
def post_automation_run(
    body: AutomationRunRequest,
    claims: Dict = Depends(require_owner),
    svc: AutomationService = Depends(get_automation_service),
):
    """Start one run of a worker cron now (FEAT-automation-admin 3c).

    An async invoke of blogWorkerLambda with the cron's own input; the request never
    waits on the run. A resent `request_id` replays the stored row and never invokes
    again. A failed invoke is a 200 with outcome `invoke_failed`: final for that id,
    and it starts no cooldown. Audited in `automation_control_events`; the worker
    stamps the row's start and end.
    """
    try:
        result = svc.run(body.job_key, body.request_id, resolve_owner(claims))
    except RunRejected as rejected:
        headers = {"Retry-After": str(rejected.retry_after_seconds)} if rejected.retry_after_seconds else None
        return _rejection(AutomationRunRejection(
            code=rejected.code, cooldown_until=rejected.cooldown_until,
            retry_after_seconds=rejected.retry_after_seconds,
        ), headers)
    except ControlConflict:
        return _rejection(AutomationRunRejection(code="request_id_conflict"))
    except SQLAlchemyError as error:
        logger.error("automation run: DB write failed (%s)", type(error).__name__)
        # The claim may have committed and the invoke may have happened; resending
        # the same request_id replays whatever was stored and never invokes twice.
        raise HTTPException(status_code=503, detail="Run unavailable; resend with the same request_id")
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return result
