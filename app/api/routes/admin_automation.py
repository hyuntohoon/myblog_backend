# app/api/routes/admin_automation.py
# FEAT-automation-admin 2b — the owner's read-only automation snapshot.
#
# Route posture (decided in 2b's security-review; see the RFC design notes):
#   - GET /api/admin/automation   owner (require_owner, in-Lambda)
#
# It rides the edge_guard GET catch-all with no infra/apigateway.tf route, as
# GET /api/todays-pick/queue does: through CloudFront a missing token is 401
# from the Cognito guard and a non-owner token is 403; on the raw API Gateway
# host edge_guard answers 403. Fail-closed either way.
#
# Opening this starts nothing: one read-only DB transaction, no enqueue, no
# AWS call (the AWS adapter is 2d).
from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, Depends

from app.api.schemas import AutomationSnapshot
from app.core.authz import require_owner
from app.di import get_automation_service
from app.services.automation_service import AutomationService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=AutomationSnapshot)
def get_automation_snapshot(
    _claims: Dict = Depends(require_owner),
    svc: AutomationService = Depends(get_automation_service),
):
    """Every catalogued job with its evidence; unwired sources render unknown."""
    return svc.snapshot()
