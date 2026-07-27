"""FIX-nightly-draft-identity follow-on: the grow-once route + the widened coercion.

Route-level pins for the two server-side guarantees the 03:00 draft agent relies on:

1. `POST /api/buckets/nightly-grow` — the agent (or the owner) may mark the owner's
   checked memos processed; a plain member may not; the target post must exist and
   be a draft; and the acting user is pinned server-side — the request body cannot
   name one (the impersonation pin).
2. `create_post` coercion, widened — a non-owner caller's editorial fields (album
   links, artists, rating, tags, editorial genres, classics, recommended tracks,
   BEST NEW) are dropped, not honored. Status was already coerced to 'draft'
   (#133); these tests pin the rest.

Auth mechanics mirror tests/test_draft_agent_identity.py: `auth.settings` is
monkeypatched on the module (it binds the singleton at import), and the
verified-claims dependency is overridden per test to act as owner / agent /
member. Everything else (edge_guard, config used by other modules) keeps the
local test env.
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.core.auth as auth
import app.core.config as cfg
from app.core.auth import require_cognito_token
from app.di import get_bucket_service, get_post_service
from app.services.bucket_service import GrowPostNotDraftError, GrowPostNotFoundError

OWNER = "0468fd3c-0000-4000-8000-000000000001"
AGENT = "64885d4c-0000-4000-8000-000000000002"
MEMBER = "aaaaaaaa-0000-4000-8000-000000000003"

ALBUM_ID = "11111111-1111-4111-8111-111111111111"
POST_ID = "22222222-2222-4222-8222-222222222222"


def _prod_settings(**kw):
    base = dict(
        ENV="prod",
        COGNITO_USER_POOL_ID="pool",
        COGNITO_REGION="ap-northeast-2",
        OWNER_SUB=OWNER,
        DRAFT_AGENT_SUB=AGENT,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _act_as(app, sub: str) -> None:
    app.dependency_overrides[require_cognito_token] = lambda: {"sub": sub}


def _pin_owner_sub_env(monkeypatch) -> None:
    """Make the route's get_settings().OWNER_SUB resolve to OWNER for this test."""
    monkeypatch.setenv("OWNER_SUB", OWNER)
    cfg.get_settings.cache_clear()


def _grow_svc(grown: int | Exception = 1) -> MagicMock:
    svc = MagicMock()
    if isinstance(grown, Exception):
        svc.grow_nightly.side_effect = grown
    else:
        svc.grow_nightly.return_value = grown
    return svc


class TestNightlyGrowRoute:
    def test_agent_can_grow(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _pin_owner_sub_env(monkeypatch)
        _act_as(app, AGENT)
        svc = _grow_svc(grown=2)
        app.dependency_overrides[get_bucket_service] = lambda: svc

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": ALBUM_ID, "post_id": POST_ID},
        )

        assert resp.status_code == 200
        assert resp.json() == {"grown": 2}
        args = svc.grow_nightly.call_args.args
        assert args[1] == OWNER  # owner pinned from settings
        assert args[2] == uuid.UUID(ALBUM_ID)
        assert args[3] == uuid.UUID(POST_ID)

    def test_owner_can_grow(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _pin_owner_sub_env(monkeypatch)
        _act_as(app, OWNER)
        app.dependency_overrides[get_bucket_service] = lambda: _grow_svc(grown=1)

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": ALBUM_ID, "post_id": POST_ID},
        )
        assert resp.status_code == 200
        assert resp.json() == {"grown": 1}

    def test_member_is_rejected_403(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _act_as(app, MEMBER)
        svc = _grow_svc()
        app.dependency_overrides[get_bucket_service] = lambda: svc

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": ALBUM_ID, "post_id": POST_ID},
        )
        assert resp.status_code == 403
        svc.grow_nightly.assert_not_called()

    def test_missing_token_is_401(self, monkeypatch, client, app):
        # No require_cognito_token override: the real dependency runs against
        # prod-shaped settings and must 401 an unauthenticated call.
        monkeypatch.setattr(auth, "settings", _prod_settings())
        svc = _grow_svc()
        app.dependency_overrides[get_bucket_service] = lambda: svc

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": ALBUM_ID, "post_id": POST_ID},
        )
        assert resp.status_code == 401
        svc.grow_nightly.assert_not_called()

    def test_unset_agent_sub_rejects_the_agent(self, monkeypatch, client, app):
        """An unset DRAFT_AGENT_SUB means *no agent exists* on this route too."""
        monkeypatch.setattr(auth, "settings", _prod_settings(DRAFT_AGENT_SUB=""))
        _act_as(app, AGENT)
        svc = _grow_svc()
        app.dependency_overrides[get_bucket_service] = lambda: svc

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": ALBUM_ID, "post_id": POST_ID},
        )
        assert resp.status_code == 403
        svc.grow_nightly.assert_not_called()

    def test_body_cannot_name_the_acting_user(self, monkeypatch, client, app):
        """The impersonation pin: extra user-ish body keys are ignored and the
        service still receives the settings-pinned owner."""
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _pin_owner_sub_env(monkeypatch)
        _act_as(app, AGENT)
        svc = _grow_svc(grown=0)
        app.dependency_overrides[get_bucket_service] = lambda: svc

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={
                "album_id": ALBUM_ID,
                "post_id": POST_ID,
                "user_id": MEMBER,
                "owner_sub": MEMBER,
            },
        )
        assert resp.status_code == 200
        assert svc.grow_nightly.call_args.args[1] == OWNER

    def test_post_not_found_maps_to_404(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _act_as(app, AGENT)
        app.dependency_overrides[get_bucket_service] = (
            lambda: _grow_svc(GrowPostNotFoundError(POST_ID))
        )

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": ALBUM_ID, "post_id": POST_ID},
        )
        assert resp.status_code == 404

    def test_non_draft_post_maps_to_409(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _act_as(app, AGENT)
        app.dependency_overrides[get_bucket_service] = (
            lambda: _grow_svc(GrowPostNotDraftError(POST_ID))
        )

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": ALBUM_ID, "post_id": POST_ID},
        )
        assert resp.status_code == 409

    def test_malformed_ids_are_422(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _act_as(app, AGENT)
        svc = _grow_svc()
        app.dependency_overrides[get_bucket_service] = lambda: svc

        resp = client.post(
            "/api/buckets/nightly-grow",
            json={"album_id": "junk", "post_id": POST_ID},
        )
        assert resp.status_code == 422
        svc.grow_nightly.assert_not_called()


def _created_post():
    p = MagicMock()
    p.id = POST_ID
    p.slug = "smoke-slug"
    return p


def _full_payload() -> dict:
    """A payload exercising every editorial field the coercion must drop."""
    return {
        "title": "smoke title",
        "body_mdx": "# body",
        "description": "d",
        "posted_date": str(date(2026, 7, 27)),
        "status": "published",
        "category": "Reviews",
        "tags": ["Tag"],
        "genre_ids": ["g-1"],
        "album_ids": [ALBUM_ID],
        "artist_ids": ["ar-1"],
        "rating": 4.5,
        "album_classics": {ALBUM_ID: True},
        "recommended_track_ids": ["t-1"],
        "subject_best_new": True,
    }


class TestCreatePostCoercion:
    def test_agent_editorial_fields_are_dropped(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _act_as(app, AGENT)
        svc = MagicMock()
        svc.create.return_value = _created_post()
        app.dependency_overrides[get_post_service] = lambda: svc

        resp = client.post("/api/posts", json=_full_payload())

        assert resp.status_code == 200
        kwargs = svc.create.call_args.kwargs
        assert kwargs["status"] == "draft"          # #133 coercion still holds
        assert kwargs["tags"] == []
        assert kwargs["genre_ids"] == []
        assert kwargs["album_ids"] == []            # never enters post_albums
        assert kwargs["artist_ids"] == []
        assert kwargs["rating"] is None
        assert kwargs["album_classics"] == {}
        assert kwargs["recommended_track_ids"] == []
        assert kwargs["subject_best_new"] is None   # never mutates albums.best_new
        # The agent's legitimate surface passes through untouched.
        assert kwargs["title"] == "smoke title"
        assert kwargs["body_mdx"] == "# body"
        assert kwargs["section_name"] == "Reviews"

    def test_owner_fields_pass_through(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _act_as(app, OWNER)
        svc = MagicMock()
        svc.create.return_value = _created_post()
        app.dependency_overrides[get_post_service] = lambda: svc

        resp = client.post("/api/posts", json=_full_payload())

        assert resp.status_code == 200
        kwargs = svc.create.call_args.kwargs
        assert kwargs["status"] == "published"
        assert kwargs["tags"] == ["Tag"]
        assert kwargs["album_ids"] == [ALBUM_ID]
        assert kwargs["rating"] == 4.5
        assert kwargs["album_classics"] == {ALBUM_ID: True}
        assert kwargs["subject_best_new"] is True

    def test_member_is_still_rejected_403(self, monkeypatch, client, app):
        monkeypatch.setattr(auth, "settings", _prod_settings())
        _act_as(app, MEMBER)
        svc = MagicMock()
        app.dependency_overrides[get_post_service] = lambda: svc

        resp = client.post("/api/posts", json=_full_payload())

        assert resp.status_code == 403
        svc.create.assert_not_called()
