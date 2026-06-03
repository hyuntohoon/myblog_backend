from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.bucket_service import (
    W_POPULARITY,
    W_RECENCY,
    BucketService,
)

TODAY = date(2026, 6, 3)


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
