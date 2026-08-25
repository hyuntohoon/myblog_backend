# PUT /api/reviews/albums/{album_id}/best-new — owner-only mark/unmark of
# `albums.best_new` from the rating surface (AlbumRatingBlock), a second entry
# point onto the same column the post editor's "BEST NEW MUSIC" button writes.
# Pattern mirrors test_todays_pick.py's TestOwnerGate: missing OWNER_SUB → 503,
# valid-but-non-owner → 403, local ENV bypasses, owner reaches the service.
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.auth import require_cognito_token
from app.services.rating_service import AlbumNotFoundError, RatingService


def _override(app, svc):
    from app.db.session import get_db
    from app.di import get_rating_service

    app.dependency_overrides[get_rating_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


def _prod_settings(owner_sub: str):
    return SimpleNamespace(
        ENV="prod",
        COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
        COGNITO_REGION="ap-northeast-2",
        OWNER_SUB=owner_sub,
    )


class TestOwnerGate:
    def test_rejects_non_owner_in_prod(self, client, app):
        import app.core.auth as auth_module

        album_id = str(uuid.uuid4())
        svc = MagicMock(spec=RatingService)
        _override(app, svc)
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "some-member"}

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.put(f"/api/reviews/albums/{album_id}/best-new", json={"best_new": True})

        assert resp.status_code == 403
        svc.set_best_new.assert_not_called()
        app.dependency_overrides.clear()

    def test_503_when_owner_sub_unset_in_prod(self, client, app):
        import app.core.auth as auth_module

        album_id = str(uuid.uuid4())
        svc = MagicMock(spec=RatingService)
        _override(app, svc)
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "anyone"}

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="")):
            resp = client.put(f"/api/reviews/albums/{album_id}/best-new", json={"best_new": True})

        assert resp.status_code == 503
        svc.set_best_new.assert_not_called()
        app.dependency_overrides.clear()

    def test_allows_the_owner_in_prod(self, client, app):
        import app.core.auth as auth_module

        album_id = str(uuid.uuid4())
        svc = MagicMock(spec=RatingService)
        svc.set_best_new.return_value = True
        _override(app, svc)
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "owner-sub"}

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.put(f"/api/reviews/albums/{album_id}/best-new", json={"best_new": True})

        assert resp.status_code == 200
        assert resp.json() == {"album_id": album_id, "best_new": True}
        svc.set_best_new.assert_called_once()
        assert svc.set_best_new.call_args.args[1:] == (uuid.UUID(album_id), True)
        app.dependency_overrides.clear()

    def test_writes_pass_in_local_env(self, client, app):
        # conftest forces ENV=local, so require_owner bypasses.
        album_id = str(uuid.uuid4())
        svc = MagicMock(spec=RatingService)
        svc.set_best_new.return_value = False
        _override(app, svc)

        resp = client.put(f"/api/reviews/albums/{album_id}/best-new", json={"best_new": False})

        assert resp.status_code == 200
        assert resp.json() == {"album_id": album_id, "best_new": False}
        app.dependency_overrides.clear()


class TestNotFoundAndMalformed:
    def test_unknown_album_is_404(self, client, app):
        album_id = str(uuid.uuid4())
        svc = MagicMock(spec=RatingService)
        svc.set_best_new.side_effect = AlbumNotFoundError(album_id)
        _override(app, svc)

        resp = client.put(f"/api/reviews/albums/{album_id}/best-new", json={"best_new": True})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_malformed_album_id_is_404_and_never_queries(self, client, app):
        svc = MagicMock(spec=RatingService)
        _override(app, svc)

        resp = client.put("/api/reviews/albums/not-a-uuid/best-new", json={"best_new": True})

        assert resp.status_code == 404
        svc.set_best_new.assert_not_called()
        app.dependency_overrides.clear()


class TestAggregateCarriesBestNew:
    def test_public_aggregate_reflects_current_best_new(self, client, app):
        album_id = str(uuid.uuid4())
        svc = MagicMock(spec=RatingService)
        svc.album_aggregate.return_value = (None, 0, [])
        svc.album_best_new.return_value = True
        _override(app, svc)

        resp = client.get(f"/api/reviews/albums/{album_id}")

        assert resp.status_code == 200
        assert resp.json()["best_new"] is True
        app.dependency_overrides.clear()
