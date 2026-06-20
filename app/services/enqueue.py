# app/services/enqueue.py
# FEAT-genre-artist-distribution Step 5 — shared best-effort enqueue helpers (통일성).
#
# Lifted from buckets.py so the 평론 버킷 and the 분석 버킷 share ONE fire-and-forget
# enqueue path: an enqueue hiccup must NEVER fail the user op (log + continue). The
# response is built from already-committed state before these run, so a rollback here
# can't corrupt it. Hard rule #9: these only enqueue/INSERT — the worker does any
# Spotify/LLM work.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def safe_enqueue_album(db, research_svc, album_id) -> None:
    """Fire-and-forget research enqueue ($0 INSERT) for one catalog album — the
    평론 버킷 auto-research path (FEAT-album-research-notes)."""
    try:
        research_svc.enqueue_album(db, str(album_id))
    except Exception:
        db.rollback()
        logger.warning(
            "research auto-enqueue failed for album %s (continuing)", album_id,
            exc_info=True,
        )


def safe_enqueue_bucket(db, research_svc, bucket) -> None:
    """Fire-and-forget research enqueue for a bucket's in-scope items."""
    try:
        n = research_svc.enqueue_bucket(db, bucket)
        if n:
            logger.info("auto-enqueued %d research row(s) for bucket %s", n, bucket.id)
    except Exception:
        db.rollback()
        logger.warning(
            "research bucket enqueue failed for bucket %s (continuing)", bucket.id,
            exc_info=True,
        )


def safe_enqueue_catalog_sync(sqs, album_sids) -> int:
    """Fire-and-forget catalog album-sync enqueue for the 분석 버킷 분류하기: send the
    distinct spotify album ids to the worker's album-sync queue, which catalogs them
    and maps S1 genres (album_genres) — after which the saved track inherits a genre.
    Best-effort (an SQS hiccup must not fail the request). Returns the count actually
    enqueued (0 when no queue is configured / on error)."""
    sids = [s for s in (album_sids or []) if s]
    if not sids:
        return 0
    try:
        return sqs.send_album_sync(sids)
    except Exception:
        logger.warning(
            "catalog-sync enqueue failed for %d album(s) (continuing)", len(sids),
            exc_info=True,
        )
        return 0
