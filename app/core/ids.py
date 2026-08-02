# app/core/ids.py
# AUDIT-2026-07-26 A-3 — parse id strings at the route boundary, not in psycopg.
#
# Album/user ids arrive from the URL as `str` and go straight into a
# `WHERE id = :x` against a uuid column. Postgres rejects a malformed value with
# InvalidTextRepresentation, which surfaces as an unhandled 500 on input that is
# entirely under the caller's control — and on public routes, unauthenticated.
#
# A string that is not a UUID cannot name a row, so "not found" is the honest
# answer; that is also what the already-correct `/api/reviews/albums/{id}` has
# always returned, which is why this helper is shared rather than copied a third
# time. Its twin lives in myblog_music `app/core/ids.py` (different repo, same
# defect class — swept together).
from __future__ import annotations

import uuid

from fastapi import HTTPException


def parse_uuid_or_404(value: str, *, detail: str = "Album not found") -> uuid.UUID:
    """Return `value` as a UUID, or raise 404 — never let it reach the driver."""
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=404, detail=detail)


def parse_uuid_list_or_400(values: list[str], *, detail: str = "malformed album id") -> list[uuid.UUID]:
    """Batch variant. 400, not 404: a batch read has no single subject to be
    "not found", and the existing `too many album_ids` guard on the same route
    already answers 400 for a caller-shaped mistake.

    All-or-nothing on purpose. Dropping the bad entries would return a map that
    is silently short one album, which reads as "no research note" — the exact
    wrong answer for a badge, and invisible to whoever sent the bad id.
    """
    out: list[uuid.UUID] = []
    for v in values:
        try:
            out.append(uuid.UUID(v))
        except (AttributeError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail=detail)
    return out
