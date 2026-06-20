"""FEAT-genre-artist-distribution Step 3 — pure unit tests for the shared counting
primitive (rank_counts) and the saved-track genre-resolution rule. No DB."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.distribution import rank_counts
from app.services.library_service import LibraryService


class TestRankCounts:
    def test_partition_sums_to_total(self):
        items, unclassified = rank_counts(["a", "b", "a", None, "c", None])
        assert dict(items) == {"a": 2, "b": 1, "c": 1}
        assert unclassified == 2
        assert sum(c for _, c in items) + unclassified == 6

    def test_sorted_by_count_desc_then_label_asc(self):
        # a:2, b:2, c:1 → the a/b tie breaks by label ascending
        items, _ = rank_counts(["b", "a", "b", "a", "c"])
        assert items == [("a", 2), ("b", 2), ("c", 1)]

    def test_weights_weight_each_item(self):
        items, unclassified = rank_counts(["rock", "pop", None], weights=[10, 3, 5])
        assert dict(items) == {"rock": 10, "pop": 3}
        assert unclassified == 5
        assert items[0] == ("rock", 10)  # play-count weighting ranks rock first

    def test_all_none(self):
        items, unclassified = rank_counts([None, None])
        assert items == []
        assert unclassified == 2

    def test_empty(self):
        assert rank_counts([]) == ([], 0)

    def test_weights_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            rank_counts(["a"], weights=[1, 2])


class TestResolveSavedGenre:
    svc = LibraryService()

    def _row(self, track_id=None, album_id=None):
        return SimpleNamespace(track_id=track_id, album_id=album_id)

    def test_track_override_wins_over_album(self):
        g = self.svc._resolve_saved_genre(
            self._row(track_id="t1", album_id="a1"),
            override_map={"t1": "K-Pop"},
            album_genre_map={"a1": "Pop"},
        )
        assert g == "K-Pop"

    def test_album_inherit_when_no_override(self):
        g = self.svc._resolve_saved_genre(
            self._row(track_id="t1", album_id="a1"),
            override_map={},
            album_genre_map={"a1": "Pop"},
        )
        assert g == "Pop"

    def test_unclassified_when_neither(self):
        assert (
            self.svc._resolve_saved_genre(self._row(track_id="t1", album_id="a1"), {}, {})
            is None
        )

    def test_uncatalogued_track_with_no_album(self):
        assert (
            self.svc._resolve_saved_genre(self._row(track_id=None, album_id=None), {}, {})
            is None
        )
