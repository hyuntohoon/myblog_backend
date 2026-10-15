from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.services.bucket_service import (
    LIBRARY_SYNC_DEBOUNCE_SECONDS,
    LIBRARY_SYNC_STALE_SECONDS,
    SPOTIFY_LIBRARY_BUCKET_KIND,
    SPOTIFY_LIBRARY_BUCKET_NAME,
    BucketService,
    library_sync_gate,
)

# FEAT-multi-user Phase 2: the spotify_library bucket is owner-scoped; mock ignores value.
OWNER_ID = uuid.UUID(int=1)


class TestLibrarySyncGate:
    """The pure decision behind POST /spotify-library/sync and the state GET's
    sync_in_flight. The locked read/write around it runs on a real engine in
    tests/integration/test_library_sync_gate_db.py."""

    NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    def _ago(self, seconds):
        return self.NOW - timedelta(seconds=seconds)

    def test_never_requested_is_queued(self):
        assert library_sync_gate(None, None, self.NOW) == "queued"

    def test_outstanding_request_is_in_flight(self):
        assert library_sync_gate(self._ago(10), None, self.NOW) == "in_flight"
        assert library_sync_gate(self._ago(10), self._ago(3600), self.NOW) == "in_flight"

    def test_stale_request_is_queued_again(self):
        assert library_sync_gate(self._ago(LIBRARY_SYNC_STALE_SECONDS + 1), None, self.NOW) == "queued"

    def test_just_finished_is_debounced(self):
        assert library_sync_gate(self._ago(40), self._ago(LIBRARY_SYNC_DEBOUNCE_SECONDS - 5), self.NOW) == "debounced"
        # a finish with no request on record (e.g. a worker-only run) debounces too
        assert library_sync_gate(None, self._ago(5), self.NOW) == "debounced"

    def test_finish_outside_window_is_queued(self):
        assert library_sync_gate(self._ago(90), self._ago(LIBRARY_SYNC_DEBOUNCE_SECONDS + 5), self.NOW) == "queued"


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
