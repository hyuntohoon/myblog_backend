# Route-level shape guard for the PUBLIC review payload (2026-07-14 audit F4.3):
# users.id IS the Cognito sub, so ReviewAuthor must never carry an `id` — clients
# identify authors (incl. "my review") by the unique handle instead.
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

_TS = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _override(app, svc):
    from app.db.session import get_db
    from app.di import get_review_service

    app.dependency_overrides[get_review_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestPublicReviewAuthorShape:
    def test_aggregate_author_has_handle_but_no_sub(self, client, app):
        album_id = str(uuid.uuid4())
        review = SimpleNamespace(
            id=uuid.uuid4(), album_id=uuid.UUID(album_id), rating=4.5,
            comment="응집력", created_at=_TS, updated_at=_TS,
        )
        user = SimpleNamespace(
            id=uuid.uuid4(),  # the Cognito sub — must NOT serialize
            handle="user-abcd1234", display_name="멤버", avatar_url=None,
        )
        svc = MagicMock()
        svc.album_aggregate.return_value = (4.5, 1, [(review, user)])
        _override(app, svc)

        resp = client.get(f"/api/reviews/albums/{album_id}")

        assert resp.status_code == 200
        author = resp.json()["reviews"][0]["author"]
        assert author["handle"] == "user-abcd1234"
        assert "id" not in author
        app.dependency_overrides.clear()
