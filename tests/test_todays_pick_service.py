"""Service-level SQL and transaction tests for the today's-pick queue."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

import app.core.kst as kst_module
from app.core.kst import kst_today
from app.services.todays_pick_service import TodaysPickService


TRACK_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
ALBUM_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
QUEUE_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


def _queue_row():
    return SimpleNamespace(
        id=QUEUE_ID,
        track_id=TRACK_ID,
        album_id=ALBUM_ID,
        title="Dior",
        artist="Pop Smoke",
        cover_url="https://i.scdn.co/cover.jpg",
        spotify_track_id="0VjIjW4GlU",
        created_at=datetime(2026, 7, 9, 12, 0, 0),
    )


def _scalar_result(*, one=None, optional=None, rows=None):
    result = MagicMock()
    if one is not None:
        result.scalar_one.return_value = one
    result.scalar_one_or_none.return_value = optional
    if rows is not None:
        result.scalars.return_value.all.return_value = rows
    return result


def test_list_queue_orders_created_at_desc():
    db = MagicMock()
    newer, older = _queue_row(), _queue_row()
    db.execute.return_value = _scalar_result(rows=[newer, older])

    rows = TodaysPickService().list_queue(db)

    assert rows == [newer, older]
    statement = db.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ORDER BY daily_pick_queue.created_at DESC" in sql


def test_add_to_queue_uses_named_conflict_and_returns_inserted_row():
    db = MagicMock()
    row = _queue_row()
    db.execute.return_value = _scalar_result(optional=row)

    result = TodaysPickService().add_to_queue(
        db,
        track_id=str(TRACK_ID),
        album_id=str(ALBUM_ID),
        title=row.title,
        artist=row.artist,
        cover_url=row.cover_url,
        spotify_track_id=row.spotify_track_id,
    )

    assert result is row
    statement = db.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT ON CONSTRAINT uq_daily_pick_queue_track DO NOTHING" in sql
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(row)


def test_add_to_queue_conflict_returns_existing_row_without_duplicate():
    db = MagicMock()
    row = _queue_row()
    db.execute.side_effect = [
        _scalar_result(optional=None),
        _scalar_result(one=row),
    ]

    result = TodaysPickService().add_to_queue(
        db,
        track_id=str(TRACK_ID),
        album_id=str(ALBUM_ID),
        title="replacement title is ignored",
        artist=row.artist,
        cover_url=None,
        spotify_track_id=row.spotify_track_id,
    )

    assert result is row
    assert db.execute.call_count == 2
    lookup = str(
        db.execute.call_args_list[1].args[0].compile(dialect=postgresql.dialect())
    )
    assert "WHERE daily_pick_queue.track_id =" in lookup
    db.commit.assert_called_once()


def test_delete_from_queue_success_and_missing():
    svc = TodaysPickService()
    row = _queue_row()
    db = MagicMock()
    db.get.return_value = row

    assert svc.delete_from_queue(db, str(QUEUE_ID)) is True
    db.delete.assert_called_once_with(row)
    db.commit.assert_called_once()

    missing_db = MagicMock()
    missing_db.get.return_value = None
    assert svc.delete_from_queue(missing_db, str(uuid.uuid4())) is False
    missing_db.delete.assert_not_called()
    missing_db.commit.assert_not_called()


def test_promote_copies_queue_columns_deletes_row_and_commits_once():
    db = MagicMock()
    queue_row = _queue_row()
    pick = MagicMock()
    db.execute.side_effect = [
        _scalar_result(optional=queue_row),
        _scalar_result(one=pick),
    ]

    result = TodaysPickService().promote_from_queue(db, str(QUEUE_ID))

    assert result is pick
    upsert = db.execute.call_args_list[1].args[0]
    params = upsert.compile(dialect=postgresql.dialect()).params
    assert params["track_id"] == str(TRACK_ID)
    assert params["album_id"] == str(ALBUM_ID)
    assert params["title"] == queue_row.title
    assert params["artist"] == queue_row.artist
    assert params["cover_url"] == queue_row.cover_url
    assert params["spotify_track_id"] == queue_row.spotify_track_id
    sql = str(upsert.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT ON CONSTRAINT uq_daily_picks_pick_date DO UPDATE" in sql
    # A-4: the day boundary is computed in Python (KST), so pick_date is a bound
    # parameter now — never Postgres' UTC CURRENT_DATE.
    assert "CURRENT_DATE" not in sql
    assert params["pick_date"] == kst_today()
    db.delete.assert_called_once_with(queue_row)
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(pick)
    db.rollback.assert_not_called()


def test_promote_unknown_does_not_mutate():
    db = MagicMock()
    db.execute.return_value = _scalar_result(optional=None)

    assert TodaysPickService().promote_from_queue(db, str(QUEUE_ID)) is None
    db.delete.assert_not_called()
    db.commit.assert_not_called()


def test_promote_delete_failure_rolls_back_pick_and_preserves_queue():
    queue_row = _queue_row()
    old_pick = {"title": "Yesterday"}
    committed_queue = {QUEUE_ID: queue_row}
    pending = {"pick": None, "delete": None}
    db = MagicMock()

    def execute(statement):
        if len(db.execute.call_args_list) == 1:
            return _scalar_result(optional=queue_row)
        pending["pick"] = {"title": queue_row.title}
        return _scalar_result(one=MagicMock())

    def fail_delete(row):
        pending["delete"] = row.id
        raise RuntimeError("forced queue delete failure")

    def rollback():
        pending["pick"] = None
        pending["delete"] = None

    db.execute.side_effect = execute
    db.delete.side_effect = fail_delete
    db.rollback.side_effect = rollback

    with pytest.raises(RuntimeError, match="forced queue delete failure"):
        TodaysPickService().promote_from_queue(db, str(QUEUE_ID))

    assert old_pick == {"title": "Yesterday"}
    assert committed_queue == {QUEUE_ID: queue_row}
    assert pending == {"pick": None, "delete": None}
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


# ── A-4: KST day boundary ───────────────────────────────────────────────────
#
# The bug: reads and writes both used Postgres `current_date`, which resolves in
# the DB session's timezone (Neon runs UTC). Between 00:00 and 09:00 KST that is
# still *yesterday* in UTC, so a pick posted at 01:00 KST was stored under
# yesterday's date — and disappeared from the home tile at 09:00, when
# `current_date` finally rolled over. Visitors in that window saw the old pick.


class _FrozenDatetime(datetime):
    """datetime whose now(tz) is pinned, so the KST boundary is testable."""

    _frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._frozen.astimezone(tz) if tz else cls._frozen


@pytest.fixture
def freeze_clock(monkeypatch):
    def _freeze(utc_moment: datetime):
        frozen = type("_F", (_FrozenDatetime,), {"_frozen": utc_moment})
        monkeypatch.setattr(kst_module, "datetime", frozen)

    return _freeze


# 2026-07-09 KST 00:00 .. 08:59 is 2026-07-08 15:00 .. 23:59 UTC — the window
# where UTC and KST disagree about which day it is.
KST_EARLY_MORNING_UTC = [
    datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc),   # 00:00 KST
    datetime(2026, 7, 8, 20, 30, tzinfo=timezone.utc),  # 05:30 KST
    datetime(2026, 7, 8, 23, 59, tzinfo=timezone.utc),  # 08:59 KST
]


@pytest.mark.parametrize("utc_moment", KST_EARLY_MORNING_UTC)
def test_kst_today_is_the_kst_day_not_the_utc_day(freeze_clock, utc_moment):
    freeze_clock(utc_moment)

    assert kst_today() == date(2026, 7, 9)
    assert utc_moment.date() == date(2026, 7, 8), "fixture sanity: UTC is a day behind"


@pytest.mark.parametrize("utc_moment", KST_EARLY_MORNING_UTC)
def test_pick_written_before_09_kst_is_read_back_the_same_day(freeze_clock, utc_moment):
    """The regression: write then read inside 00:00–09:00 KST must agree.

    Both halves go through the same Python-side KST date, so the pick the owner
    posts at 01:00 KST is the pick the home tile finds at 01:05 — and it does not
    vanish when UTC rolls over at 09:00.
    """
    freeze_clock(utc_moment)
    svc = TodaysPickService()

    write_db = MagicMock()
    write_db.execute.return_value = _scalar_result(one=MagicMock())
    svc.upsert(
        write_db,
        track_id=str(TRACK_ID),
        album_id=str(ALBUM_ID),
        title="Dior",
        artist="Pop Smoke",
        cover_url=None,
        spotify_track_id="0VjIjW4GlU",
    )
    write_compiled = write_db.execute.call_args.args[0].compile(
        dialect=postgresql.dialect()
    )
    assert "CURRENT_DATE" not in str(write_compiled), (
        "the write fell back to Postgres CURRENT_DATE — that is UTC, so a pick "
        "posted before 09:00 KST lands on the previous day (A-4)"
    )
    written = write_compiled.params["pick_date"]

    read_db = MagicMock()
    read_db.execute.return_value = _scalar_result(optional=None)
    svc.get_today(read_db)
    read_compiled = read_db.execute.call_args.args[0].compile(
        dialect=postgresql.dialect()
    )
    assert "CURRENT_DATE" not in str(read_compiled), (
        "the read fell back to Postgres CURRENT_DATE (UTC) — it would miss a "
        "pick written for the KST day (A-4)"
    )
    read = read_compiled.params["pick_date_1"]

    assert written == read == date(2026, 7, 9), (
        f"write stored {written} but the read looked for {read}"
    )


def test_promote_from_queue_pins_the_same_kst_day(freeze_clock):
    """The queue promote path shares `_daily_pick_upsert_stmt`, so it must land
    on the same KST day as a direct POST."""
    freeze_clock(datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc))  # 03:00 KST
    db = MagicMock()
    db.execute.side_effect = [
        _scalar_result(optional=_queue_row()),
        _scalar_result(one=MagicMock()),
    ]

    TodaysPickService().promote_from_queue(db, str(QUEUE_ID))

    upsert = db.execute.call_args_list[1].args[0]
    params = upsert.compile(dialect=postgresql.dialect()).params
    assert params["pick_date"] == date(2026, 7, 9)
