"""FEAT-multi-user-accounts Phase 1 — ReviewService unit tests: upsert
create-vs-edit branching, the per-member daily create cap, album-existence
guard, and delete paths. DB is a MagicMock Session (branching logic, not pool
semantics); the aggregate/profile SQL is exercised in
tests/integration/test_review_service_db.py against a real engine."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.services.review_service import (
    AlbumNotFoundError,
    ReviewNotFoundError,
    ReviewRateLimitError,
    ReviewService,
)

MEMBER_ID = uuid.UUID("6f1b2f6e-6b1a-4c3e-9a2e-2b7c8d9e0f11")
ALBUM_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _svc_with_user():
    """ReviewService whose UserService.get_or_create returns a stable member row."""
    users = MagicMock()
    user = MagicMock()
    user.id = MEMBER_ID
    users.get_or_create.return_value = user
    return ReviewService(users=users), user


class TestUpsert:
    def test_new_review_under_cap_is_inserted(self):
        svc, user = _svc_with_user()
        db = MagicMock()
        db.get.return_value = MagicMock()          # album exists
        db.scalar.side_effect = [None, 0]          # no existing review; 0 recent

        review, ret_user = svc.upsert(
            db, MEMBER_ID, {}, ALBUM_ID, 4.5, "solid", daily_cap=50
        )

        assert ret_user is user
        assert review.user_id == MEMBER_ID
        assert review.album_id == ALBUM_ID
        assert float(review.rating) == 4.5
        db.add.assert_called_once()
        db.commit.assert_called_once()

    def test_daily_cap_blocks_new_review(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = MagicMock()          # album exists
        db.scalar.side_effect = [None, 50]         # no existing; already at cap

        with pytest.raises(ReviewRateLimitError):
            svc.upsert(db, MEMBER_ID, {}, ALBUM_ID, 3.0, None, daily_cap=50)
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_edit_existing_review_ignores_cap(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        existing = MagicMock()
        db.get.return_value = MagicMock()          # album exists
        db.scalar.side_effect = [existing]         # existing review found; NO count query

        svc.upsert(db, MEMBER_ID, {}, ALBUM_ID, 2.5, "changed my mind", daily_cap=50)

        assert float(existing.rating) == 2.5
        assert existing.comment == "changed my mind"
        db.add.assert_not_called()                 # edit in place, not a new row
        db.commit.assert_called_once()

    def test_missing_album_raises_not_found(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = None                 # album absent

        with pytest.raises(AlbumNotFoundError):
            svc.upsert(db, MEMBER_ID, {}, ALBUM_ID, 5.0, None, daily_cap=50)
        db.commit.assert_not_called()


class TestDeleteOwn:
    def test_deletes_own_review(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        review = MagicMock()
        db.scalar.return_value = review

        svc.delete_own(db, MEMBER_ID, ALBUM_ID)
        db.delete.assert_called_once_with(review)
        db.commit.assert_called_once()

    def test_missing_review_raises_not_found(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.scalar.return_value = None

        with pytest.raises(ReviewNotFoundError):
            svc.delete_own(db, MEMBER_ID, ALBUM_ID)
        db.delete.assert_not_called()


class TestDeleteAny:
    def test_owner_deletes_any_review(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        review = MagicMock()
        db.get.return_value = review

        svc.delete_any(db, uuid.uuid4())
        db.delete.assert_called_once_with(review)
        db.commit.assert_called_once()

    def test_missing_review_raises_not_found(self):
        svc, _ = _svc_with_user()
        db = MagicMock()
        db.get.return_value = None

        with pytest.raises(ReviewNotFoundError):
            svc.delete_any(db, uuid.uuid4())
        db.delete.assert_not_called()
