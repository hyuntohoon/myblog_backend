# app/core/kst.py
"""KST day-boundary helper — the single definition of "today" for this service.

Site convention: the database stores UTC, but a *day* is KST wall-clock, and the
boundary is computed in Python — never with Postgres' `current_date`.

Both halves matter. `current_date` (and `date.today()` in a Lambda) resolves
against the session/host timezone, which is UTC in every environment we run:
Neon, the Lambda runtime, and CI. A KST day therefore starts 9 hours late, so
anything written or read between 00:00 and 09:00 KST lands on the previous day.
That is A-4: a pick posted at 01:00 KST was stored under yesterday's date and
vanished from the home tile at 09:00.

Computing it in Python also keeps the SQLite-backed unit tests honest — they
behave identically to Postgres instead of silently agreeing with whatever the
test session's timezone happens to be.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def kst_today() -> date:
    """Today's calendar date on the KST wall clock."""
    return datetime.now(KST).date()
