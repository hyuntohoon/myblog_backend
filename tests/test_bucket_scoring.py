from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.bucket_service import (
    W_POPULARITY,
    W_RECENCY,
    BucketService,
    TypedMembershipNotEnabledError,
)

TODAY = date(2026, 6, 3)


class TestAddItemTypeGate:
    def test_nonalbum_item_type_raises_before_insert(self):
        # FEAT-pocket-buckit Step 3: the service gate rejects a non-album write up front
        # (a non-album INSERT would violate album_id NOT NULL until the Step-6 relax). The
        # MagicMock db lets get_bucket return a truthy bucket so we reach the item_type
        # branch — proving the gate, not a missing-bucket 404, is what raises.
        svc = BucketService()
        db = MagicMock()  # db.query(...).filter(...).first() → a truthy mock bucket
        with pytest.raises(TypedMembershipNotEnabledError):
            svc.add_item(db, "bk-1", album_id="alb-1", item_type="track")

    def test_album_item_type_passes_the_gate(self):
        # An album write goes past the gate and proceeds to the Album lookup (which returns
        # None on the mock → AlbumNotFoundError), i.e. it did NOT raise the typed-gate error.
        from app.services.bucket_service import AlbumNotFoundError

        svc = BucketService()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            SimpleNamespace(id="bk-1"),  # get_bucket → a bucket
            None,                          # Album lookup → missing
        ]
        with pytest.raises(AlbumNotFoundError):
            svc.add_item(db, "bk-1", album_id="alb-x", item_type="album")


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
