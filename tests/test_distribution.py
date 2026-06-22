"""FEAT-genre-artist-distribution Step 3 — pure unit tests for the shared counting
primitive (rank_counts) and the saved-track genre-resolution rule. No DB."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.distribution import (
    expand_credits,
    is_va_compilation,
    rank_counts,
    resolve_saved_artist_names,
    split_artist_names,
)
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


class TestSplitArtistNames:
    """The denormalized-string fallback (FIX-analysis-artist-attribution) — used
    only when no album_id is present to join album_artists."""

    def test_splits_comma_joined(self):
        assert split_artist_names("A, B, C") == ["A", "B", "C"]

    def test_single_artist(self):
        assert split_artist_names("Solo Artist") == ["Solo Artist"]

    def test_strips_and_drops_blanks(self):
        assert split_artist_names("A ,  , B ") == ["A", "B"]

    def test_none_and_empty(self):
        assert split_artist_names(None) == []
        assert split_artist_names("") == []

    def test_comma_in_name_over_splits(self):
        # Documented lossy edge of the fallback: a comma-bearing name is split.
        # The catalogued album_artists path (exact) avoids this — hence preferred.
        assert split_artist_names("Tyler, the Creator") == ["Tyler", "the Creator"]


class TestExpandCredits:
    """Per-artist credit expansion: a collab row credits each artist once (role is
    always NULL → no primary/featured weighting)."""

    def test_collab_credits_each_artist(self):
        labels, weights = expand_credits([(["A", "B"], 1)])
        assert labels == ["A", "B"]
        assert weights == [1, 1]

    def test_weight_propagates_to_each(self):
        labels, weights = expand_credits([(["A", "B"], 5)])
        assert labels == ["A", "B"]
        assert weights == [5, 5]  # each artist gets the row's full play_count

    def test_no_names_is_one_unclassified_credit(self):
        assert expand_credits([(None, 3)]) == ([None], [3])
        assert expand_credits([([], 2)]) == ([None], [2])

    def test_drops_blank_names(self):
        labels, weights = expand_credits([(["A", "", None], 1)])
        assert labels == ["A"]
        assert weights == [1]


class TestArtistDistributionDefragmentation:
    """End-to-end (pure): expand_credits → rank_counts de-fragments a collab that
    the old comma-joined label would have split off from the solo count."""

    def test_collab_no_longer_fragments_solo_count(self):
        # Old behavior counted 'A, B' as its own label, so solo 'A' = 1 while the
        # collab bucket = 2 — A's real presence (3) was fragmented.
        old_items, _ = rank_counts(["A, B", "A", "A, B"])
        assert dict(old_items) == {"A, B": 2, "A": 1}

        # New: each collab credits A and B individually → A aggregates to 3.
        labels, weights = expand_credits([(["A", "B"], 1), (["A"], 1), (["A", "B"], 1)])
        new_items, unclassified = rank_counts(labels, weights)
        assert dict(new_items) == {"A": 3, "B": 2}
        assert unclassified == 0

    def test_play_weighted_collab_credits_full_count_each(self):
        # An album played 10× by [A, B] and one played 4× by [B] → A:10, B:14.
        labels, weights = expand_credits([(["A", "B"], 10), (["B"], 4)])
        items, unclassified = rank_counts(labels, weights)
        assert dict(items) == {"B": 14, "A": 10}
        assert unclassified == 0

    def test_album_with_no_artists_weights_into_unclassified(self):
        labels, weights = expand_credits([(["A"], 3), (None, 5)])
        items, unclassified = rank_counts(labels, weights)
        assert dict(items) == {"A": 3}
        assert unclassified == 5


class TestIsVaCompilation:
    def test_single_various_artists(self):
        assert is_va_compilation(["Various Artists"]) is True

    def test_all_various_artists(self):
        assert is_va_compilation(["Various Artists", "Various Artists"]) is True

    def test_real_artist_is_not_va(self):
        assert is_va_compilation(["NewJeans"]) is False

    def test_mixed_is_not_va(self):
        # A compilation hosted by a real artist keeps album_artists (not a pure VA).
        assert is_va_compilation(["Various Artists", "DJ Host"]) is False

    def test_empty_and_none(self):
        assert is_va_compilation([]) is False
        assert is_va_compilation(None) is False


class TestResolveSavedArtistNames:
    """Hybrid saved-track attribution (FIX-analysis-artist-attribution): album_artists
    by default, VA compilation → track_artists → denorm split, uncatalogued → split."""

    def test_normal_album_uses_album_artists(self):
        # Exact, comma-safe — and ignores the denormalized string entirely.
        assert resolve_saved_artist_names(["A", "B"], None, "A, B") == ["A", "B"]

    def test_va_album_falls_back_to_track_artists(self):
        out = resolve_saved_artist_names(["Various Artists"], ["Coogie", "CHANGMO"], "x")
        assert out == ["Coogie", "CHANGMO"]  # real performers, not 'Various Artists'

    def test_va_album_without_track_artists_splits_denorm(self):
        out = resolve_saved_artist_names(["Various Artists"], None, "BIG Naughty, Coogie")
        assert out == ["BIG Naughty", "Coogie"]

    def test_uncatalogued_splits_denorm(self):
        assert resolve_saved_artist_names(None, None, "Solo") == ["Solo"]

    def test_catalogued_but_empty_album_artists_splits_denorm(self):
        assert resolve_saved_artist_names([], None, "X, Y") == ["X", "Y"]

    def test_va_album_with_no_fallback_at_all_is_empty(self):
        # VA album, no track_artists, no denorm → unclassified (empty list).
        assert resolve_saved_artist_names(["Various Artists"], None, None) == []
