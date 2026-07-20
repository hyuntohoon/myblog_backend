# app/clients/sqs_client.py
# Thin SQS producer for the backend. The backend sends async job triggers the worker
# consumes (hard rule #9 — a user-facing endpoint must never call Spotify
# synchronously):
#   {"job": "spotify_refresh"}        — manual "지금 새로고침" listening-refresh
#   {"job": "spotify_library_sync"}   — explicit Spotify Library bucket reconcile
#   {"job": "spotify_follow_import", "user_id": ...}
#                                     — owner followed-artists snapshot import
#                                       (FEAT-for-you-releases Step 2)
#   {"album_ids": [...]}              — catalog album-sync for the 분석 버킷 분류하기
#                                       (worker _process_batch → AlbumSyncService +
#                                       S1 genre mapping)
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)

# Catalog album-sync messages are chunked to match the worker's own producer
# (sqs_producer._MAX_PER_MESSAGE = 20). The worker processes one message per Lambda
# invocation (120s timeout), so a large 분류하기 set (e.g. 712 uncatalogued albums)
# must span MANY messages — one 712-album batch would time out → SQS retry → DLQ.
_ALBUM_SYNC_CHUNK = 20


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

    def send_library_sync(self) -> bool:
        """Enqueue a Spotify-Library sync job ({"job": "spotify_library_sync"}); the
        worker consumes it and performs the actual Spotify reads/diffs/writes (hard
        rule #9 — the user-facing endpoint must never call Spotify synchronously).
        Returns False (and logs) when no queue is configured — e.g. local dev — so
        the endpoint degrades to a no-op instead of 500ing."""
        if not self.queue_url:
            logger.info("SQS_QUEUE_URL unset; library sync not enqueued (local/dev)")
            return False
        import boto3

        sqs = boto3.client(
            "sqs",
            region_name=settings.AWS_DEFAULT_REGION,
            endpoint_url=(settings.LOCALSTACK_ENDPOINT or None),
        )
        sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"job": "spotify_library_sync"}),
        )
        logger.info("enqueued spotify library sync job")
        return True

    def send_follow_import(self, user_id: str) -> bool:
        """Enqueue an owner followed-artists snapshot import ({"job":
        "spotify_follow_import", "user_id": ...}); the worker pages Spotify
        /me/following, matches catalog artists into user_artist_tracks, and
        catalog-ingests the rest (hard rule #9 — the user-facing endpoint must
        never call Spotify synchronously). Returns False (and logs) when no queue
        is configured — e.g. local dev — so the endpoint degrades to a no-op
        instead of 500ing."""
        if not self.queue_url:
            logger.info("SQS_QUEUE_URL unset; follow import not enqueued (local/dev)")
            return False
        import boto3

        sqs = boto3.client(
            "sqs",
            region_name=settings.AWS_DEFAULT_REGION,
            endpoint_url=(settings.LOCALSTACK_ENDPOINT or None),
        )
        sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"job": "spotify_follow_import", "user_id": user_id}),
        )
        logger.info("enqueued spotify follow import job")
        return True

    def send_album_sync(self, album_sids) -> int:
        """Enqueue catalog album-sync jobs ({"album_ids": [...]}); the worker consumes
        them (_process_batch → AlbumSyncService) to catalog the albums and map their S1
        genres. Used by the 분석 버킷 분류하기 (hard rule #9 — the worker does the Spotify
        read). Chunked at _ALBUM_SYNC_CHUNK ids/message (one worker invocation per
        message, 120s timeout) so a large set spans many messages instead of one batch
        that would time out. Returns the count enqueued, or 0 (and logs) when no queue
        is configured — e.g. local dev — so the endpoint degrades to a no-op."""
        sids = [s for s in (album_sids or []) if s]
        if not sids:
            return 0
        if not self.queue_url:
            logger.info("SQS_QUEUE_URL unset; album sync not enqueued (local/dev)")
            return 0
        import boto3

        sqs = boto3.client(
            "sqs",
            region_name=settings.AWS_DEFAULT_REGION,
            endpoint_url=(settings.LOCALSTACK_ENDPOINT or None),
        )
        for i in range(0, len(sids), _ALBUM_SYNC_CHUNK):
            sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps({"album_ids": sids[i : i + _ALBUM_SYNC_CHUNK]}),
            )
        n_msgs = -(-len(sids) // _ALBUM_SYNC_CHUNK)
        logger.info(
            "enqueued album sync for %d album(s) in %d message(s)", len(sids), n_msgs
        )
        return len(sids)


@dataclass
class SpotifyConnectionStatus:
    """Token *validity* for the 연동 tab (D30), not mere presence. ``needs_reauth`` is
    set by the worker when a refresh hit invalid_grant (token revoked/expired);
    ``last_successful_refresh_at`` is when the token last worked."""

    connected: bool = False
    needs_reauth: bool = False
    last_successful_refresh_at: datetime | None = None


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


# Connection status changes only when the worker rotates/invalidates the token (~hourly
# at most), so cache it across warm Lambda invocations rather than hitting Secrets
# Manager on every 연동-tab open.
_CONN_TTL_SEC = 300.0
_conn_cache: dict = {"val": None, "ts": 0.0}


def get_spotify_connection_status() -> SpotifyConnectionStatus:
    """Read the 연동-tab status from Secrets Manager myblog/spotify (TTL-cached).

    ``connected`` = a refresh token is stored; ``needs_reauth`` = the worker's last
    refresh was rejected (invalid_grant → "재인증 필요"); ``last_successful_refresh_at`` =
    when the token last worked. Never returns the token itself."""
    import time

    arn = settings.SPOTIFY_SECRETS_ARN
    param = settings.SPOTIFY_SECRETS_PARAM
    if not arn and not param:
        return SpotifyConnectionStatus()
    now = time.time()
    if _conn_cache["val"] is not None and now - _conn_cache["ts"] < _CONN_TTL_SEC:
        return _conn_cache["val"]
    try:
        import boto3

        # SSM (SECRETS_PARAM) preferred → Secrets Manager fallback (CHORE-secrets-ssm-migration)
        if param:
            ssm = boto3.client("ssm", region_name=settings.AWS_DEFAULT_REGION)
            payload = json.loads(ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"])
        else:
            sm = boto3.client("secretsmanager", region_name=settings.AWS_DEFAULT_REGION)
            payload = json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])
        status = SpotifyConnectionStatus(
            connected=bool(payload.get("refresh_token") or payload.get("SPOTIFY_REFRESH_TOKEN")),
            needs_reauth=bool(payload.get("needs_reauth")),
            last_successful_refresh_at=_parse_dt(payload.get("last_successful_refresh_at")),
        )
        _conn_cache.update(val=status, ts=now)
        return status
    except Exception as e:  # pragma: no cover - IAM/network failure path
        logger.error("Failed to read Spotify connection status: %s", e)
        return SpotifyConnectionStatus()


def is_spotify_connected() -> bool:
    """Back-compat: whether a refresh token is stored (presence only)."""
    return get_spotify_connection_status().connected
