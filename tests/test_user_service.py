"""FEAT-multi-user-accounts 0d — UserService unit tests: handle derivation
(ck_users_handle_format mirror), lazy-create collision retry, and the same-id
race. DB is a MagicMock Session (provisioning logic, not pool semantics — no
session-lifecycle change here)."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.user_service import UserService, derive_handle

MEMBER_ID = uuid.UUID("6f1b2f6e-6b1a-4c3e-9a2e-2b7c8d9e0f11")


def _integrity_error():
    return IntegrityError("stmt", {}, Exception("uq_users_handle"))


class TestDeriveHandle:
    def test_email_local_part_normalized(self):
        assert derive_handle({"email": "Foo.Bar+x@example.com"}, MEMBER_ID) == "foo-bar-x"

    def test_no_email_falls_back_to_sub(self):
        assert derive_handle({}, MEMBER_ID) == f"user-{MEMBER_ID.hex[:8]}"

    def test_too_short_local_part_falls_back(self):
        assert derive_handle({"email": "ab@x.com"}, MEMBER_ID).startswith("user-")

    def test_long_local_part_truncated_to_valid(self):
        h = derive_handle({"email": ("a" * 40) + "@x.com"}, MEMBER_ID)
        assert len(h) <= 30 and h == "a" * 30

    def test_result_always_matches_check_constraint(self):
        import re

        from app.services.user_service import HANDLE_PATTERN

        for email in ["--weird--@x.com", "한글만@x.com", "_x_@x.com", None]:
            claims = {"email": email} if email else {}
            assert re.fullmatch(HANDLE_PATTERN, derive_handle(claims, MEMBER_ID))


class TestGetOrCreate:
    def test_existing_row_returned_without_insert(self):
        db = MagicMock()
        existing = MagicMock()
        db.get.return_value = existing

        assert UserService().get_or_create(db, MEMBER_ID, {}) is existing
        db.add.assert_not_called()

    def test_provisions_with_claim_defaults(self):
        db = MagicMock()
        db.get.return_value = None
        claims = {
            "sub": str(MEMBER_ID),
            "email": "listener@example.com",
            "name": "Listener One",
            "picture": "https://idp/avatar.jpg",
        }

        user = UserService().get_or_create(db, MEMBER_ID, claims)

        assert user.id == MEMBER_ID
        assert user.email == "listener@example.com"
        assert user.handle == "listener"
        assert user.display_name == "Listener One"
        assert user.avatar_url == "https://idp/avatar.jpg"
        db.commit.assert_called_once()

    def test_handle_collision_retries_with_sub_suffix(self):
        db = MagicMock()
        db.get.return_value = None
        db.commit.side_effect = [_integrity_error(), None]

        user = UserService().get_or_create(db, MEMBER_ID, {"email": "listener@x.com"})

        assert user.handle == f"listener-{MEMBER_ID.hex[:5]}"
        db.rollback.assert_called_once()

    def test_same_id_race_returns_winner_row(self):
        db = MagicMock()
        winner = MagicMock()
        # First get: absent → insert → IntegrityError → second get: winner row.
        db.get.side_effect = [None, winner]
        db.commit.side_effect = _integrity_error()

        assert UserService().get_or_create(db, MEMBER_ID, {}) is winner


class TestUpdateMe:
    def test_updates_only_given_fields(self):
        db = MagicMock()
        row = MagicMock()
        db.get.return_value = row

        UserService().update_me(db, MEMBER_ID, {}, display_name="새 이름")

        assert row.display_name == "새 이름"
        db.commit.assert_called_once()

    def test_handle_conflict_raises_handle_taken(self):
        from app.services.user_service import HandleTakenError

        db = MagicMock()
        db.get.return_value = MagicMock()
        db.commit.side_effect = _integrity_error()

        with pytest.raises(HandleTakenError):
            UserService().update_me(db, MEMBER_ID, {}, handle="taken")
        db.rollback.assert_called_once()


class TestDeleteMe:
    def test_deletes_existing_row(self):
        db = MagicMock()
        row = MagicMock()
        db.get.return_value = row

        assert UserService().delete_me(db, MEMBER_ID) is True
        db.delete.assert_called_once_with(row)

    def test_missing_row_is_idempotent_false(self):
        db = MagicMock()
        db.get.return_value = None

        assert UserService().delete_me(db, MEMBER_ID) is False
        db.delete.assert_not_called()
