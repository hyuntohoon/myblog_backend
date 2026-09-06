"""YouTube Data API v3 — `videos.list` only.

FEAT-youtube-playback-provider Step A3.

TWIN NOTICE. This is the second of three YouTube clients in this system, and
they are deliberately NOT identical in surface:

  myblog_music/app/clients/youtube_client.py   search.list + videos.list  (A2)
  myblog_backend/app/clients/youtube_client.py videos.list ONLY           (A3, this file)
  myblog_worker/worker/clients/youtube_client.py videos.list ONLY         (A5)

`search.list` is DISCOVERY and costs 100 units against a shared daily pool; it
belongs in exactly one place, and that place is music. Backend and worker only
ever ask "what is the current state of these ids", which is `videos.list` at 1
unit for 50 ids. Giving this file a `search_videos` it does not need would put
the expensive call one autocomplete away from a route that must not make it.

What the three DO share is the error taxonomy and the credential handling, and
that shared part is the twin: **a fix to the quota/rate-limit split, to the
header-based auth, or to the malformed-payload hardening belongs in every copy
in the same change** (workspace CLAUDE.md, "Recurring bug classes" — a
pattern-shaped bug in duplicated cross-repo code gets swept, not patched once).

THE KEY GOES IN A HEADER, NEVER THE QUERY STRING. httpx logs `request.url` at
INFO on every completed request, so a `?key=` credential is one
`basicConfig(level=INFO)` away from CloudWatch. This is not hypothetical: the
A2 revision of the music client shipped that shape and a review caught it.

WHY BACKEND VERIFIES AT ALL, rather than trusting what the client posts. The
mapping table is GLOBAL — one row per track for every member. If the write took
`embeddable` / `privacy_status` from the request body, one member could write a
mapping that every other member then resolves and fails to play. Server-side
verification is what makes a global row safe to accept from a non-owner.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Twin of the music client's split. The daily pool resets at midnight Pacific;
# a rate limit clears in seconds. Collapsing them tells a caller to come back
# tomorrow for a condition that resolves in a moment.
DAILY_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})

VIDEOS_LIST_MAX_IDS = 50


class YouTubeError(RuntimeError):
    """Any YouTube Data API failure that is not one of the cases below."""


class YouTubeQuotaExhausted(YouTubeError):
    """The DAILY quota is spent. Not retryable today."""


class YouTubeRateLimited(YouTubeError):
    """A short-window rate limit. Retryable in seconds."""


class YouTubeNotConfigured(YouTubeError):
    """No API key configured. Fails closed — never writes an unverified mapping."""


class YouTubeClient:
    """Stateless. Holds no token and no session."""

    def _headers(self) -> Dict[str, str]:
        key = settings.YOUTUBE_API_KEY
        if not key:
            raise YouTubeNotConfigured(
                "YOUTUBE_API_KEY is empty. Set YOUTUBE_SECRETS_PARAM to the SSM "
                "SecureString holding it (see app/core/config.py)."
            )
        return {"X-goog-api-key": key}

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        headers = self._headers()
        url = f"{settings.YOUTUBE_API_BASE}/{endpoint}"
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=settings.YOUTUBE_HTTP_TIMEOUT)
        except httpx.HTTPError as e:
            logger.warning("YouTube %s transport failure: %s", endpoint, type(e).__name__)
            raise YouTubeError(f"YouTube {endpoint} request failed") from None

        if r.status_code == 403:
            reason = self._error_reason(r)
            if reason in DAILY_QUOTA_REASONS:
                logger.warning("YouTube %s daily quota exhausted (reason=%s)", endpoint, reason)
                raise YouTubeQuotaExhausted(reason)
            if reason in RATE_LIMIT_REASONS:
                logger.warning("YouTube %s rate limited (reason=%s)", endpoint, reason)
                raise YouTubeRateLimited(reason)
            logger.warning("YouTube %s forbidden (reason=%s)", endpoint, reason)
            raise YouTubeError(f"YouTube {endpoint} forbidden: {reason}")

        if r.status_code >= 400:
            logger.warning("YouTube %s HTTP %s (reason=%s)", endpoint, r.status_code, self._error_reason(r))
            raise YouTubeError(f"YouTube {endpoint} returned HTTP {r.status_code}")

        # A 200 whose body is not the documented shape is an UPSTREAM failure and
        # must surface as YouTubeError (-> 502), never as a bare
        # AttributeError/ValueError the route does not catch (-> 500).
        try:
            body = r.json()
        except Exception:
            raise YouTubeError(f"YouTube {endpoint} returned a non-JSON body") from None
        if not isinstance(body, dict):
            raise YouTubeError(f"YouTube {endpoint} returned {type(body).__name__}, expected an object")
        return body

    @staticmethod
    def _items(body: Dict[str, Any]) -> List[Dict[str, Any]]:
        """`items`, with every non-object entry dropped.

        Factored out rather than inlined so the hardening is ONE GREPPABLE METHOD
        in all three copies. The TWIN NOTICE names malformed-payload handling as
        the shared surface, and a sweep cannot find a shape that is a method in
        one copy and inline in another.
        """
        raw = body.get("items")
        if not isinstance(raw, list):
            return []
        return [it for it in raw if isinstance(it, dict)]

    @staticmethod
    def _error_reason(r: httpx.Response) -> str:
        """Never raises: an error path that can itself fail turns a quota answer into a 500."""
        try:
            body = r.json()
            errors = ((body or {}).get("error") or {}).get("errors") or []
            reason = (errors[0] or {}).get("reason")
        except Exception:
            return ""
        # str() is not enough — a non-string `reason` (an object, say) is
        # unhashable, and the caller's `reason in DAILY_QUOTA_REASONS` would
        # raise TypeError, turning the intended 502 into a 500. This helper is
        # documented "never raises"; that promise is only useful if its RETURN
        # TYPE is also guaranteed.
        return reason if isinstance(reason, str) else ""

    def list_videos(self, video_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """One `videos.list` call (1 unit) for up to 50 ids, keyed by video id.

        An id ABSENT from the response is deleted, private, or never existed —
        the API reports it by OMISSION, never as an error. Callers must treat
        absence as a state, not a failure.
        """
        ids = [v for v in (video_ids or []) if v]
        if not ids:
            return {}
        if len(ids) > VIDEOS_LIST_MAX_IDS:
            raise ValueError(
                f"videos.list accepts at most {VIDEOS_LIST_MAX_IDS} ids, got {len(ids)}"
            )
        body = self._get("videos", {"part": "snippet,status,contentDetails", "id": ",".join(ids)})
        # `isinstance(str)`, not truthiness: a non-string `id` (an object) is
        # unhashable and would raise TypeError building this dict — the exact
        # sibling asymmetry `_items` exists to prevent.
        return {it["id"]: it for it in self._items(body) if isinstance(it.get("id"), str)}


youtube = YouTubeClient()
