"""FEAT-multi-user-accounts Phase 1 + FEAT-album-review-authoring Step 1 —
RatingService unit tests: upsert create-vs-edit branching, the PARTIAL-change
semantics that let two surfaces write different facets of one state, the
per-member daily create cap, the album-existence guard, and the delete paths
that must preserve a private editorial mark. DB is a MagicMock Session
(branching logic, not pool semantics); the aggregate/profile SQL and the
public-visibility filter are exercised in
tests/integration/test_rating_service_db.py against a real engine."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.services.rating_service import (
    AlbumNotFoundError,
    RatingNotFoundError,
    RatingRateLimitError,
    RatingService,
)

MEMBER_ID = uuid.UUID("6f1b2f6e-6b1a-4c3e-9a2e-2b7c8d9e0f11")
ALBUM_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _svc_with_user():
    """RatingService whose UserService.get_or_create returns a stable member row."""
    users = MagicMock()
    user = MagicMock()
    user.id = MEMBER_ID
    users.get_or_create.return_value = user
    return RatingService(users=users), user


def _existing(rating=4.0, comment=None, review_candidate=False):
    """A stored state. Explicit attributes — a bare MagicMock makes every flag
    truthy, which silently inverts the keep-the-row branches below."""
    row = MagicMock()
    row.rating = rating
    row.comment = comment
    row.review_candidate = review_candidate
    return row


class TestUpsert:
    def test_new_rating_under_cap_is_inserted(self):
        svc, user = _svc_with_user()
        db = MagicMock()
        db.get.return_value = MagicMock()          # album exists
        db.scalar.side_effect = [None, 0]          # no existing state; 0 recent

        state, ret_user = svc.upsert(
            db, MEMBER_ID, {}, ALBUM_ID,
            {"rating": 4.5, "comment": "solid"}, daily_cap=50,
        )

        assert ret_user is user
        assert state.user_id == MEMBER_ID
        assert state.album_id == ALBUM_ID
        assert float(state.rating) == 4.5
        assert state.review_candidate is False
        db.add.assert_called_once()
        db.commit.assert_called_once()

    def test_daily_cap_blocks_new_rating(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = MagicMock()          # album exists
        db.scalar.side_effect = [None, 50]         # no existing; already at cap

        with pytest.raises(RatingRateLimitError):
            svc.upsert(db, MEMBER_ID, {}, ALBUM_ID, {"rating": 3.0}, daily_cap=50)
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_edit_existing_state_ignores_cap(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        existing = _existing()
        db.get.return_value = MagicMock()          # album exists
        db.scalar.side_effect = [existing]         # existing state; NO count query

        svc.upsert(
            db, MEMBER_ID, {}, ALBUM_ID,
            {"rating": 2.5, "comment": "changed my mind"}, daily_cap=50,
        )

        assert float(existing.rating) == 2.5
        assert existing.comment == "changed my mind"
        db.add.assert_not_called()                 # edit in place, not a new row
        db.commit.assert_called_once()

    def test_missing_album_raises_not_found(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = None                 # album absent

        with pytest.raises(AlbumNotFoundError):
            svc.upsert(db, MEMBER_ID, {}, ALBUM_ID, {"rating": 5.0}, daily_cap=50)
        db.commit.assert_not_called()

    # ── partial semantics: two surfaces, one state ───────────────────────────

    def test_marking_a_candidate_leaves_the_rating_alone(self):
        """The bucket board flips the mark without sending a rating. If the
        absent fields were treated as nulls it would silently wipe the 평가 —
        the exact collision the partial contract exists to prevent."""
        svc, _ = _svc_with_user()
        db = MagicMock()
        existing = _existing(rating=4.0, comment="세상에서 가장 조용한 소란")
        db.get.return_value = MagicMock()
        db.scalar.side_effect = [existing]

        svc.upsert(
            db, MEMBER_ID, {}, ALBUM_ID, {"review_candidate": True}, daily_cap=50
        )

        assert existing.review_candidate is True
        assert float(existing.rating) == 4.0
        assert existing.comment == "세상에서 가장 조용한 소란"
        db.delete.assert_not_called()

    def test_rating_an_album_leaves_the_mark_alone(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        existing = _existing(rating=None, review_candidate=True)
        db.get.return_value = MagicMock()
        db.scalar.side_effect = [existing]

        svc.upsert(db, MEMBER_ID, {}, ALBUM_ID, {"rating": 3.5}, daily_cap=50)

        assert float(existing.rating) == 3.5
        assert existing.review_candidate is True

    def test_mark_can_be_created_without_a_rating(self):
        """C6: the mark can be placed before listening, so a state may be born
        with no rating at all."""
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = MagicMock()
        db.scalar.side_effect = [None, 0]

        state, _ = svc.upsert(
            db, MEMBER_ID, {}, ALBUM_ID, {"review_candidate": True}, daily_cap=50
        )

        assert state.rating is None
        assert state.comment is None
        assert state.review_candidate is True
        db.add.assert_called_once()

    def test_clearing_the_rating_clears_its_one_liner(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        existing = _existing(rating=4.0, comment="한 줄", review_candidate=True)
        db.get.return_value = MagicMock()
        db.scalar.side_effect = [existing]

        svc.upsert(db, MEMBER_ID, {}, ALBUM_ID, {"rating": None}, daily_cap=50)

        assert existing.rating is None
        assert existing.comment is None            # the CHECK would reject the pair
        db.delete.assert_not_called()              # the mark keeps the row alive

    def test_clearing_the_last_facet_deletes_the_state(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        existing = _existing(rating=4.0, comment="한 줄", review_candidate=False)
        db.get.return_value = MagicMock()
        db.scalar.side_effect = [existing]

        state, _ = svc.upsert(
            db, MEMBER_ID, {}, ALBUM_ID, {"rating": None}, daily_cap=50
        )

        assert state is None                       # the route answers 204
        db.delete.assert_called_once_with(existing)

    def test_empty_change_on_a_missing_state_creates_nothing(self):
        """Unmarking an album that was never marked is a no-op — and must not
        burn a create against the daily cap."""
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = MagicMock()
        db.scalar.side_effect = [None]             # no existing; NO count query

        state, _ = svc.upsert(
            db, MEMBER_ID, {}, ALBUM_ID, {"review_candidate": False}, daily_cap=50
        )

        assert state is None
        db.add.assert_not_called()
        db.commit.assert_not_called()


class TestDeleteOwn:
    def test_deletes_own_rating(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        state = _existing(rating=4.0)
        db.scalar.return_value = state

        svc.delete_own(db, MEMBER_ID, ALBUM_ID)
        db.delete.assert_called_once_with(state)
        db.commit.assert_called_once()

    def test_deleting_a_rating_keeps_the_private_mark(self):
        """Removing a public 평가 is not a request to forget that the album is
        an editorial candidate."""
        svc, _ = _svc_with_user()
        db = MagicMock()
        state = _existing(rating=4.0, comment="한 줄", review_candidate=True)
        db.scalar.return_value = state

        svc.delete_own(db, MEMBER_ID, ALBUM_ID)

        db.delete.assert_not_called()
        assert state.rating is None
        assert state.comment is None
        assert state.review_candidate is True
        db.commit.assert_called_once()

    def test_missing_rating_raises_not_found(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.scalar.return_value = None

        with pytest.raises(RatingNotFoundError):
            svc.delete_own(db, MEMBER_ID, ALBUM_ID)
        db.delete.assert_not_called()

    def test_mark_only_state_has_no_rating_to_delete(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.scalar.return_value = _existing(rating=None, review_candidate=True)

        with pytest.raises(RatingNotFoundError):
            svc.delete_own(db, MEMBER_ID, ALBUM_ID)
        db.delete.assert_not_called()


class TestDeleteAny:
    def test_owner_deletes_any_rating(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        state = _existing(rating=4.0)
        db.get.return_value = state

        svc.delete_any(db, uuid.uuid4())
        db.delete.assert_called_once_with(state)
        db.commit.assert_called_once()

    def test_moderation_does_not_erase_the_authors_mark(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        state = _existing(rating=1.0, review_candidate=True)
        db.get.return_value = state

        svc.delete_any(db, uuid.uuid4())

        db.delete.assert_not_called()
        assert state.rating is None
        assert state.review_candidate is True

    def test_missing_rating_raises_not_found(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = None

        with pytest.raises(RatingNotFoundError):
            svc.delete_any(db, uuid.uuid4())
        db.delete.assert_not_called()
