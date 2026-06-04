# app/clients/sqs_client.py
# Thin SQS producer for the backend. The only message the backend sends is the
# manual "지금 새로고침" listening-refresh trigger ({"job": "spotify_refresh"}); the
# worker consumes it and performs the actual Spotify read (hard rule #9 — the
# user-facing endpoint must never call Spotify synchronously).
from __future__ import annotations

import json
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


class SqsClient:
    def __init__(self, queue_url: str | None = None) -> None:
        self.queue_url = queue_url if queue_url is not None else settings.SQS_QUEUE_URL

    def send_listening_refresh(self) -> bool:
        """Enqueue a listening-refresh job. Returns False (and logs) when no queue
        is configured — e.g. local dev — so the endpoint degrades to a no-op
        instead of 500ing."""
        if not self.queue_url:
            logger.info("SQS_QUEUE_URL unset; listening refresh not enqueued (local/dev)")
            return False
        import boto3

        sqs = boto3.client(
            "sqs",
            region_name=settings.AWS_DEFAULT_REGION,
            endpoint_url=(settings.LOCALSTACK_ENDPOINT or None),
        )
        sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"job": "spotify_refresh"}),
        )
        logger.info("enqueued listening refresh job")
        return True


# Connection status changes at most once per admin bootstrap, so cache it across
# warm Lambda invocations rather than hitting Secrets Manager on every 연동-tab open.
_CONN_TTL_SEC = 300.0
_conn_cache: dict = {"val": False, "ts": 0.0}


def is_spotify_connected() -> bool:
    """Whether a Spotify refresh token is stored in Secrets Manager myblog/spotify.
    Read for the 연동 tab status (TTL-cached); never returns the token itself."""
    import time

    arn = settings.SPOTIFY_SECRETS_ARN
    if not arn:
        return False
    now = time.time()
    if now - _conn_cache["ts"] < _CONN_TTL_SEC and _conn_cache["ts"] > 0:
        return _conn_cache["val"]
    try:
        import boto3

        sm = boto3.client("secretsmanager", region_name=settings.AWS_DEFAULT_REGION)
        payload = json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])
        val = bool(payload.get("refresh_token") or payload.get("SPOTIFY_REFRESH_TOKEN"))
        _conn_cache.update(val=val, ts=now)
        return val
    except Exception as e:  # pragma: no cover - IAM/network failure path
        logger.error("Failed to read Spotify connection status: %s", e)
        return False
