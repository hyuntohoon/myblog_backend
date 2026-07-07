# app/clients/cognito_client.py
# FEAT-multi-user-accounts 0d — Cognito-side account deletion (개인정보보호법
# obligation: DELETE /api/me removes the identity, not just the DB row).
from __future__ import annotations

import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class CognitoDeleteError(Exception):
    """Cognito-side deletion failed. Route maps to 502 and leaves the DB row
    intact, so a client retry converges (the re-lookup below is idempotent)."""


def delete_cognito_user(sub: str) -> bool:
    """Delete the pool user whose `sub` matches. Returns False when the user is
    already gone (idempotent retry path). local/dev: no-op.

    `sub` is not a Cognito Username, so resolve it via ListUsers first —
    callers pass a str(uuid.UUID), so the filter value can't be injected.
    """
    settings = get_settings()
    if settings.ENV in ("local", "dev"):
        return False
    if not settings.COGNITO_USER_POOL_ID:
        # Fail closed like verify_token — never pretend the deletion happened.
        logger.error("COGNITO_USER_POOL_ID unset while ENV=%s", settings.ENV)
        raise CognitoDeleteError("auth not configured")

    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client("cognito-idp", region_name=settings.COGNITO_REGION)
    try:
        resp = client.list_users(
            UserPoolId=settings.COGNITO_USER_POOL_ID,
            Filter=f'sub = "{sub}"',
            Limit=1,
        )
        users = resp.get("Users", [])
        if not users:
            return False
        client.admin_delete_user(
            UserPoolId=settings.COGNITO_USER_POOL_ID,
            Username=users[0]["Username"],
        )
        return True
    except (BotoCoreError, ClientError) as e:
        logger.error("cognito deletion failed for sub=%s: %s", sub, e)
        raise CognitoDeleteError(str(e)) from e
