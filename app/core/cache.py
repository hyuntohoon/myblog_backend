"""Cache-Control values for public, idempotent DB-read endpoints.

Twin of `myblog_music/app/core/cache.py` (same rules apply — a change to the
policy here should be checked against the twin in the same sweep). Headers turn
on browser caching directly; the CloudFront `/api/*` behavior is
CachingDisabled, so this is browser-cache only.

Apply ONLY to 200 responses — errors must stay uncached.

PERF-home-genre-payload (audit E-1): `/api/genres/tree` shipped 292 KB gzip
with no cache header on every homepage visit while its music-service siblings
all cached. The genre taxonomy is owner-edited and changes rarely, so it gets
the longer detail-tier TTL.
"""
from __future__ import annotations

# Genre taxonomy reads (/api/genres/tree, /api/genres/roots): near-static,
# owner-edited. Same value as the music twin's DETAIL_CACHE_CONTROL.
GENRE_CACHE_CONTROL = "public, max-age=300, stale-while-revalidate=120"
