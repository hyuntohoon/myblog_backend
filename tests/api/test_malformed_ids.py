# AUDIT-2026-07-26 A-3 — a malformed id must not become a 500.
#
# Album ids reach a uuid column. Before this guard, `/api/research/albums/xxx`
# handed "xxx" to psycopg, Postgres raised InvalidTextRepresentation, and the
# caller got an unhandled 500 on input entirely under their control — on routes
# reachable without a JWT. Measured against prod 2026-08-02 before the fix:
#
#   500  /api/research/albums/not-a-uuid
#   500  /api/research/status?album_ids=not-a-uuid
#   404  /api/reviews/albums/not-a-uuid      ← already correct, hence the sweep
#
# The assertion that matters is not only the status code: the service must never
# be called at all, because "did not reach the driver" is the actual fix.
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.ids import parse_uuid_list_or_400, parse_uuid_or_404

BAD = "not-a-uuid"


def _override_research(app, svc):
    from app.db.session import get_db
    from app.di import get_research_service

    app.dependency_overrides[get_research_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


def _override_ratings(app, svc):
    from app.db.session import get_db
    from app.di import get_rating_service

    app.dependency_overrides[get_rating_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestResearchRoutes:
    def test_get_note_with_malformed_id_is_404_and_never_queries(self, client, app):
        svc = MagicMock()
        _override_research(app, svc)

        resp = client.get(f"/api/research/albums/{BAD}")

        assert resp.status_code == 404
        svc.get_research.assert_not_called()
        app.dependency_overrides.clear()

    def test_trigger_with_malformed_id_is_404_and_never_queries(self, client, app):
        svc = MagicMock()
        _override_research(app, svc)

        resp = client.post(f"/api/research/albums/{BAD}", json={})

        assert resp.status_code == 404
        svc.trigger.assert_not_called()
        app.dependency_overrides.clear()

    def test_status_map_with_one_malformed_id_is_400_and_never_queries(self, client, app):
        svc = MagicMock()
        _override_research(app, svc)

        # One bad entry among good ones — the batch is rejected whole, so nobody
        # mistakes a silently-short map for "these albums have no research note".
        good = "11111111-1111-4111-8111-111111111111"
        resp = client.get(f"/api/research/status?album_ids={good},{BAD}")

        assert resp.status_code == 400
        svc.status_map.assert_not_called()
        app.dependency_overrides.clear()


class TestRatingRoutes:
    """These were already 404 — pinned so the shared helper cannot regress them."""

    def test_public_aggregate_with_malformed_id_is_404(self, client, app):
        svc = MagicMock()
        _override_ratings(app, svc)

        resp = client.get(f"/api/reviews/albums/{BAD}")

        assert resp.status_code == 404
        svc.album_aggregate.assert_not_called()
        app.dependency_overrides.clear()

    def test_owner_delete_with_malformed_review_id_is_404(self, client, app):
        svc = MagicMock()
        _override_ratings(app, svc)

        resp = client.delete(f"/api/reviews/{BAD}")

        assert resp.status_code == 404
        svc.delete_any.assert_not_called()
        app.dependency_overrides.clear()


class TestHelper:
    def test_parses_a_real_uuid(self):
        import uuid

        u = uuid.uuid4()
        assert parse_uuid_or_404(str(u)) == u

    @pytest.mark.parametrize("bad", ["", "not-a-uuid", "123", None, "11111111-1111-4111-8111"])
    def test_rejects_anything_else_as_404(self, bad):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            parse_uuid_or_404(bad)
        assert e.value.status_code == 404

    def test_list_variant_rejects_as_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            parse_uuid_list_or_400(["11111111-1111-4111-8111-111111111111", "nope"])
        assert e.value.status_code == 400

    def test_list_variant_is_all_or_nothing(self):
        """A partial result would read as 'no research note' on the good ids."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            parse_uuid_list_or_400(["nope"])
