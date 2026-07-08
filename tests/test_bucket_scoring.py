from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.bucket_service import (
    W_POPULARITY,
    W_RECENCY,
    BucketService,
)

TODAY = date(2026, 6, 3)
# FEAT-multi-user Phase 2: add_item is now user-scoped; the mock ignores the value.
UID = uuid.UUID(int=1)


class TestAddItemBranching:
    """FEAT-pocket-buckit Step 6: the STEP-2 relax (V30/V31) is live, so add_item branches by
    item_type. These exercise the branch + per-kind dedup + typed-target lookups with a
    MagicMock db (no real session): chain.first() returns get_bucket then any typed lookup,
    chain.order_by().all() returns the existing items."""

    @staticmethod
    def _db(first_results, existing=()):
        db = MagicMock()
        chain = db.query.return_value.filter.return_value
        chain.first.side_effect = list(first_results)
        chain.order_by.return_value.all.return_value = list(existing)
        return db

    def test_album_path_still_taken_for_album(self):
        # An album write proceeds to the Album lookup (None on the mock → AlbumNotFoundError),
        # i.e. it routes through the unchanged album path.
        from app.services.bucket_service import AlbumNotFoundError

        svc = BucketService()
        db = self._db([SimpleNamespace(id="bk-1"), None])
        with pytest.raises(AlbumNotFoundError):
            svc.add_item(db, UID, "bk-1",item_type="album", album_id="alb-x")

    def test_track_missing_raises_track_not_found(self):
        from app.services.bucket_service import TrackNotFoundError

        svc = BucketService()
        db = self._db([SimpleNamespace(id="bk-1"), None], existing=[])
        with pytest.raises(TrackNotFoundError):
            svc.add_item(db, UID, "bk-1",item_type="track", track_id="trk-x")

    def test_track_inserts_typed_fields(self):
        svc = BucketService()
        db = self._db([SimpleNamespace(id="bk-1"), SimpleNamespace(id="trk-1")], existing=[])
        item = svc.add_item(db, UID, "bk-1",item_type="track", track_id="trk-1")
        assert item.item_type == "track"
        assert str(item.track_id) == "trk-1"
        assert item.album_id is None
        assert db.add.called and db.commit.called

    def test_track_duplicate_raises(self):
        from app.services.bucket_service import DuplicateItemError

        svc = BucketService()
        existing = [SimpleNamespace(item_type="track", track_id="trk-1", album_id=None, position=0)]
        db = self._db([SimpleNamespace(id="bk-1"), SimpleNamespace(id="trk-1")], existing=existing)
        with pytest.raises(DuplicateItemError):
            svc.add_item(db, UID, "bk-1",item_type="track", track_id="trk-1")

    def test_playback_allows_duplicate(self):
        # playback (queue) allows duplicate tracks (D8) — a dup does NOT raise.
        svc = BucketService()
        existing = [SimpleNamespace(item_type="playback", track_id="trk-1", album_id=None, position=0)]
        db = self._db([SimpleNamespace(id="bk-1"), SimpleNamespace(id="trk-1")], existing=existing)
        item = svc.add_item(db, UID, "bk-1",item_type="playback", track_id="trk-1")
        assert item.item_type == "playback"
        assert str(item.track_id) == "trk-1"

    def test_review_missing_raises(self):
        from app.services.bucket_service import ReviewTargetNotFoundError

        svc = BucketService()
        db = self._db([SimpleNamespace(id="bk-1"), None], existing=[])
        with pytest.raises(ReviewTargetNotFoundError):
            svc.add_item(db, UID, "bk-1",item_type="review", review_target_id="p-x")

    def test_snapshot_writes_membership_and_append_only_side_row(self):
        from myblog_shared_db.models import BucketItemSnapshot, ReviewBucketItem

        svc = BucketService()
        db = self._db([SimpleNamespace(id="bk-1")], existing=[])  # snapshot needs no typed lookup
        snap = SimpleNamespace(
            kind="period", as_of=datetime(2026, 6, 24, tzinfo=timezone.utc),
            frozen={"top": 1}, metric="plays", range_from=None, range_to=None,
            unit="count", total=10.0, unresolved=0, unclassified=0, source_album_ids=[],
        )
        item = svc.add_item(db, UID, "bk-1",item_type="snapshot", snapshot=snap)
        assert item.item_type == "snapshot"
        assert item.album_id is None and item.track_id is None
        added = [type(c.args[0]) for c in db.add.call_args_list]
        assert ReviewBucketItem in added and BucketItemSnapshot in added
        assert db.flush.called and db.commit.called


def _album(release_date=None, popularity=None):
    return SimpleNamespace(release_date=release_date, popularity=popularity)


class TestRecencyScore:
    def test_today_release_scores_one(self):
        assert BucketService._recency_score(TODAY, today=TODAY) == 1.0

    def test_future_release_clamped_to_one(self):
        assert BucketService._recency_score(date(2026, 7, 1), today=TODAY) == 1.0

    def test_missing_date_scores_zero(self):
        assert BucketService._recency_score(None, today=TODAY) == 0.0

    def test_old_release_beyond_window_scores_zero(self):
        # > 2 years old → clamped to 0.
        assert BucketService._recency_score(date(2020, 1, 1), today=TODAY) == 0.0

    def test_decays_linearly(self):
        one_year = date(2025, 6, 3)
        score = BucketService._recency_score(one_year, today=TODAY)
        # ~1 year of a 2-year window → roughly 0.5.
        assert 0.45 < score < 0.55


class TestPopularityScore:
    def test_normalizes_to_unit(self):
        assert BucketService._popularity_score(80) == 0.8

    def test_missing_scores_zero(self):
        assert BucketService._popularity_score(None) == 0.0

    def test_clamped_above_100(self):
        assert BucketService._popularity_score(150) == 1.0


class TestScoreAndReason:
    def test_weighted_blend(self):
        album = _album(release_date=TODAY, popularity=50)
        score, _ = BucketService._score(album, today=TODAY)
        # recency=1.0, popularity=0.5
        assert abs(score - (W_RECENCY * 1.0 + W_POPULARITY * 0.5)) < 1e-9

    def test_recency_dominant_reason_is_sinbo(self):
        album = _album(release_date=TODAY, popularity=10)
        _, reason = BucketService._score(album, today=TODAY)
        assert reason == "신보"

    def test_popularity_dominant_reason_is_ingi(self):
        # Old but very popular album → popularity signal wins.
        album = _album(release_date=date(2021, 1, 1), popularity=95)
        _, reason = BucketService._score(album, today=TODAY)
        assert reason == "인기"

    def test_no_signal_reason_is_none(self):
        album = _album(release_date=None, popularity=None)
        score, reason = BucketService._score(album, today=TODAY)
        assert score == 0.0
        assert reason is None
