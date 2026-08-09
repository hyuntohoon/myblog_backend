from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.services.bucket_service import (
    LIBRARY_SYNC_DEBOUNCE_SECONDS,
    SPOTIFY_LIBRARY_BUCKET_KIND,
    SPOTIFY_LIBRARY_BUCKET_NAME,
    BucketService,
)

# FEAT-multi-user Phase 2: the spotify_library bucket is owner-scoped; mock ignores value.
OWNER_ID = uuid.UUID(int=1)


class TestLibrarySyncDebounce:
    """The server-side debounce is derived from max(last_synced_at). A sync within
    the window makes a fresh POST a no-op; never-synced (None) is never debounced.
    library_last_synced_at is mocked so no DB engine is needed."""

    def _svc_with_last(self, last):
        svc = BucketService()
        svc.library_last_synced_at = MagicMock(return_value=last)
        return svc

    def test_never_synced_not_debounced(self):
        svc = self._svc_with_last(None)
        assert svc.library_sync_debounced(MagicMock()) is False

    def test_recent_sync_is_debounced(self):
        now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=LIBRARY_SYNC_DEBOUNCE_SECONDS - 5)
        svc = self._svc_with_last(last)
        assert svc.library_sync_debounced(MagicMock(), now=now) is True

    def test_old_sync_not_debounced(self):
        now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=LIBRARY_SYNC_DEBOUNCE_SECONDS + 5)
        svc = self._svc_with_last(last)
        assert svc.library_sync_debounced(MagicMock(), now=now) is False

    def test_naive_last_synced_is_coerced_to_utc(self):
        # A tz-naive DB value must not raise on comparison (coerced to UTC).
        now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        last_naive = (now - timedelta(seconds=5)).replace(tzinfo=None)
        svc = self._svc_with_last(last_naive)
        assert svc.library_sync_debounced(MagicMock(), now=now) is True


class TestGetOrCreateLibraryBucket:
    def test_returns_existing_without_creating(self):
        svc = BucketService()
        db = MagicMock()
        existing = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = existing

        result = svc.get_or_create_spotify_library_bucket(db, OWNER_ID)

        assert result is existing
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_creates_when_absent(self):
        svc = BucketService()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        # max(position) lookup → -1 so the new bucket lands at position 0.
        db.execute.return_value.scalar_one.return_value = -1

        result = svc.get_or_create_spotify_library_bucket(db, OWNER_ID)

        # A new ReviewBucket was added/committed with the special kind + name.
        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        assert added.kind == SPOTIFY_LIBRARY_BUCKET_KIND
        assert added.name == SPOTIFY_LIBRARY_BUCKET_NAME
        assert added.position == 0
        db.commit.assert_called_once()
        assert result is added


class TestGetSpotifyLibraryState:
    def test_reads_bucket_timestamp_and_rows(self):
        svc = BucketService()
        bucket = MagicMock()
        rows = [MagicMock(), MagicMock()]
        last = datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)

        db = MagicMock()
        # First .query(...).filter(...).first() → the bucket.
        db.query.return_value.filter.return_value.first.return_value = bucket
        svc.library_last_synced_at = MagicMock(return_value=last)
        svc.list_spotify_library_albums = MagicMock(return_value=rows)

        got_bucket, got_last, got_rows = svc.get_spotify_library_state(db, OWNER_ID)

        assert got_bucket is bucket
        assert got_last is last
        assert got_rows is rows

    def test_bucket_lookup_is_scoped_to_the_caller(self):
        # BUG-25 regression: the bucket query must filter on the caller's
        # owner_id, not just kind — a bare kind-only filter would hand back
        # whichever spotify_library bucket happens to be .first(), which is
        # exactly the "no scoping" bug this fix closes.
        svc = BucketService()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        svc.library_last_synced_at = MagicMock(return_value=None)
        svc.list_spotify_library_albums = MagicMock(return_value=[])

        svc.get_spotify_library_state(db, OWNER_ID)

        filter_args = db.query.return_value.filter.call_args.args
        assert len(filter_args) == 2, (
            "expected both a kind filter and a user_id filter on the bucket lookup"
        )
