"""FEAT-album-review-authoring Step 1 — route-level guards for the user-album
state.

The load-bearing property is a PRIVACY one: `review_candidate` is the product's
first piece of private user data (RFC C6 — a mark others can see becomes a
promise, and a promise can't be made lightly). It lives in the same table as the
public 평가, so the only thing separating them is that public responses are built
from a different schema and a filtered query. These tests pin both halves:

  - the public album payload carries no trace of the mark, and
  - the private one is served only on the author's own JWT route.

The query-level half (`rating IS NOT NULL` on every public read) is pinned
against a real engine in tests/integration/test_rating_service_db.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.auth import require_cognito_token

_TS = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
_SUB = "0468fd3c-1111-2222-3333-444455556666"


def _state(album_id, rating=4.0, comment=None, review_candidate=False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        album_id=uuid.UUID(album_id),
        rating=rating,
        comment=comment,
        review_candidate=review_candidate,
        created_at=_TS,
        updated_at=_TS,
    )


def _album(album_id, title="어떤 앨범", cover_url="https://img/cover.jpg"):
    return SimpleNamespace(id=uuid.UUID(album_id), title=title, cover_url=cover_url)


def _override(app, svc, *, authed=True):
    from app.db.session import get_db
    from app.di import get_rating_service

    app.dependency_overrides[get_rating_service] = lambda: svc
    # The candidate route runs primary_artist_map against this db for real (it is
    # not part of the mocked service), so its query has to resolve to a real,
    # empty list rather than a MagicMock the route would then try to unpack.
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    app.dependency_overrides[get_db] = lambda: db
    if authed:
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": _SUB}


class TestPrivacyOfTheMark:
    def test_public_album_payload_never_carries_the_mark(self, client, app):
        """The public list is built from AlbumRatingResponse, which has no field
        for the mark — so even a marked row publishes only its rating."""
        album_id = str(uuid.uuid4())
        row = _state(album_id, rating=4.5, comment="한 줄", review_candidate=True)
        user = SimpleNamespace(
            id=uuid.uuid4(), handle="user-abcd1234", display_name="멤버", avatar_url=None
        )
        svc = MagicMock()
        svc.album_aggregate.return_value = (4.5, 1, [(row, user)])
        _override(app, svc, authed=False)

        resp = client.get(f"/api/reviews/albums/{album_id}")

        assert resp.status_code == 200
        assert "review_candidate" not in resp.text
        app.dependency_overrides.clear()

    def test_my_states_are_scoped_to_the_caller(self, client, app):
        """There is no way to ask for someone else's states: the member id comes
        from the verified token, never from the request."""
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.my_states.return_value = [_state(album_id, rating=None, review_candidate=True)]
        _override(app, svc)

        resp = client.get("/api/me/album-states")

        assert resp.status_code == 200
        body = resp.json()["states"]
        assert body[0]["album_id"] == album_id
        assert body[0]["review_candidate"] is True
        assert body[0]["rating"] is None
        assert svc.my_states.call_args.args[1] == uuid.UUID(_SUB)
        app.dependency_overrides.clear()

    def test_my_states_can_be_filtered_to_one_album(self, client, app):
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.my_states.return_value = []
        _override(app, svc)

        resp = client.get(f"/api/me/album-states?album_id={album_id}")

        assert resp.status_code == 200
        assert svc.my_states.call_args.args[2] == uuid.UUID(album_id)
        app.dependency_overrides.clear()

    def test_malformed_album_filter_matches_nothing(self, client, app):
        """A bad id is an empty result, not a 500 — the album overlay passes
        whatever id it holds."""
        svc = MagicMock()
        _override(app, svc)

        resp = client.get("/api/me/album-states?album_id=not-a-uuid")

        assert resp.status_code == 200
        assert resp.json()["states"] == []
        svc.my_states.assert_not_called()
        app.dependency_overrides.clear()


class TestReviewCandidateQueue:
    """Step 2 — the harvest side of the mark. Same privacy class as the mark
    itself, so the scoping tests are repeated here rather than assumed."""

    def test_the_queue_is_scoped_to_the_caller(self, client, app):
        """No handle parameter exists, and the member id comes from the verified
        token — the same guarantee as /album-states, re-pinned on a second route
        that returns the same private class of data."""
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.my_review_candidates.return_value = [
            (_state(album_id, rating=None, review_candidate=True), _album(album_id))
        ]
        _override(app, svc)

        resp = client.get("/api/me/review-candidates")

        assert resp.status_code == 200
        assert svc.my_review_candidates.call_args.args[1] == uuid.UUID(_SUB)
        app.dependency_overrides.clear()

    def test_a_queue_row_carries_enough_to_render(self, client, app):
        """A mark can sit on an album that is in no bucket and has no rating, so
        the row is the only source of a title — without these fields the queue
        would need one album fetch per mark."""
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.my_review_candidates.return_value = [
            (_state(album_id, rating=None, review_candidate=True), _album(album_id))
        ]
        _override(app, svc)

        row = client.get("/api/me/review-candidates").json()["candidates"][0]

        assert row["album_id"] == album_id
        assert row["album_title"] == "어떤 앨범"
        assert row["album_cover_url"] == "https://img/cover.jpg"
        assert row["rating"] is None
        # No artist rows in this fixture — the fields resolve to null, not an error.
        assert row["artist_id"] is None
        assert row["artist_name"] is None
        app.dependency_overrides.clear()

    def test_an_already_rated_candidate_keeps_its_rating(self, client, app):
        """Marked-and-rated is a real state (mark first, listen, rate, still to
        write). The queue shows the work already done instead of hiding it."""
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.my_review_candidates.return_value = [
            (
                _state(album_id, rating=4.5, comment="한 줄", review_candidate=True),
                _album(album_id),
            )
        ]
        _override(app, svc)

        row = client.get("/api/me/review-candidates").json()["candidates"][0]

        assert row["rating"] == 4.5
        assert row["comment"] == "한 줄"
        app.dependency_overrides.clear()

    def test_an_empty_queue_is_an_empty_list(self, client, app):
        """The prod state on the day this shipped. Empty is a normal answer, not
        a 404 — the surface renders its own empty state from it."""
        svc = MagicMock()
        svc.my_review_candidates.return_value = []
        _override(app, svc)

        resp = client.get("/api/me/review-candidates")

        assert resp.status_code == 200
        assert resp.json()["candidates"] == []
        app.dependency_overrides.clear()

    def test_the_queue_needs_a_token(self, client, app, monkeypatch):
        """It is private data, so it must be behind require_cognito_token.

        ENV is 'local' for the whole suite, which is exactly the bypass that
        would hide a missing guard — the test lifts it for this one call so the
        assertion is about the route's wiring, not the test environment.
        """
        from app.core import auth as auth_mod

        svc = MagicMock()
        _override(app, svc, authed=False)
        monkeypatch.setattr(auth_mod.settings, "ENV", "prod")
        # A configured pool, so the answer is a plain 401 rather than the
        # fail-closed 503 that a missing pool id (correctly) produces.
        monkeypatch.setattr(auth_mod.settings, "COGNITO_USER_POOL_ID", "ap-northeast-2_test")

        resp = client.get("/api/me/review-candidates")

        assert resp.status_code == 401
        svc.my_review_candidates.assert_not_called()
        app.dependency_overrides.clear()


class TestPutState:
    def test_put_sends_only_the_fields_the_caller_set(self, client, app):
        """The bucket board flips the mark alone; `rating` must not reach the
        service as an implicit null, or it would wipe the 평가."""
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.upsert.return_value = (
            _state(album_id, rating=4.0, review_candidate=True),
            MagicMock(),
        )
        _override(app, svc)

        resp = client.put(
            f"/api/reviews/albums/{album_id}", json={"review_candidate": True}
        )

        assert resp.status_code == 200
        assert svc.upsert.call_args.args[4] == {"review_candidate": True}
        assert resp.json()["review_candidate"] is True
        app.dependency_overrides.clear()

    def test_explicit_nulls_are_instructions_to_clear(self, client, app):
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.upsert.return_value = (None, MagicMock())
        _override(app, svc)

        resp = client.put(
            f"/api/reviews/albums/{album_id}", json={"rating": None, "comment": None}
        )

        assert svc.upsert.call_args.args[4] == {"rating": None, "comment": None}
        # nothing left to store → the row is gone
        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_empty_body_is_rejected(self, client, app):
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        _override(app, svc)

        resp = client.put(f"/api/reviews/albums/{album_id}", json={})

        assert resp.status_code == 422
        svc.upsert.assert_not_called()
        app.dependency_overrides.clear()

    def test_one_liner_over_the_cap_is_rejected(self, client, app):
        """60 chars (RFC Step 1). The cap is what makes a 평가 feel lighter than
        a 평론 — if it only lived in the frontend it would not be a rule."""
        from app.api.schemas import RATING_COMMENT_MAX

        assert RATING_COMMENT_MAX == 60
        album_id = str(uuid.uuid4())
        svc = MagicMock()
        _override(app, svc)

        resp = client.put(
            f"/api/reviews/albums/{album_id}",
            json={"rating": 4.0, "comment": "가" * (RATING_COMMENT_MAX + 1)},
        )

        assert resp.status_code == 422
        svc.upsert.assert_not_called()
        app.dependency_overrides.clear()

    def test_one_liner_at_the_cap_is_accepted(self, client, app):
        from app.api.schemas import RATING_COMMENT_MAX

        album_id = str(uuid.uuid4())
        svc = MagicMock()
        svc.upsert.return_value = (_state(album_id), MagicMock())
        _override(app, svc)

        resp = client.put(
            f"/api/reviews/albums/{album_id}",
            json={"rating": 4.0, "comment": "가" * RATING_COMMENT_MAX},
        )

        assert resp.status_code == 200
        app.dependency_overrides.clear()
