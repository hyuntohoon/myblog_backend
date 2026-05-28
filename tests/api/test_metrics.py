from __future__ import annotations

from unittest.mock import MagicMock

from app.di import get_metrics_repository


class TestBatchMetrics:
    def test_returns_repo_values(self, client, app):
        mock_repo = MagicMock()
        mock_repo.get_many.return_value = {
            "post-a": {"likes": 3, "comments": 1},
            "post-b": {"likes": 0, "comments": 0},
        }
        app.dependency_overrides[get_metrics_repository] = lambda: mock_repo

        resp = client.post("/api/metrics/batch", json={"slugs": ["post-a", "post-b"]})

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "data": {
                "post-a": {"likes": 3, "comments": 1},
                "post-b": {"likes": 0, "comments": 0},
            }
        }
        app.dependency_overrides.clear()

    def test_empty_slugs_returns_empty_data(self, client, app):
        mock_repo = MagicMock()
        mock_repo.get_many.return_value = {}
        app.dependency_overrides[get_metrics_repository] = lambda: mock_repo

        resp = client.post("/api/metrics/batch", json={"slugs": []})

        assert resp.status_code == 200
        assert resp.json() == {"data": {}}
        app.dependency_overrides.clear()

    def test_missing_slug_defaults_to_zero(self, client, app):
        """Repo contract: return {likes:0, comments:0} for unknown slugs (no 404)."""
        mock_repo = MagicMock()
        mock_repo.get_many.return_value = {"ghost": {"likes": 0, "comments": 0}}
        app.dependency_overrides[get_metrics_repository] = lambda: mock_repo

        resp = client.post("/api/metrics/batch", json={"slugs": ["ghost"]})

        assert resp.status_code == 200
        assert resp.json()["data"]["ghost"] == {"likes": 0, "comments": 0}
        app.dependency_overrides.clear()


class TestSqlMetricsRepository:
    def test_get_many_zero_pads_missing_rows(self):
        """LEFT JOIN can omit slugs with no posts row → ensure caller still gets {0,0}."""
        from app.repositories.metrics_repository import SqlMetricsRepository

        repo = SqlMetricsRepository()
        fake_db = MagicMock()
        fake_db.execute.return_value.mappings.return_value.all.return_value = [
            {"slug": "post-a", "likes": 5, "comments": 2},
        ]

        out = repo.get_many(fake_db, ["post-a", "post-b"])

        assert out == {
            "post-a": {"likes": 5, "comments": 2},
            "post-b": {"likes": 0, "comments": 0},
        }

    def test_get_many_empty_slugs_skips_query(self):
        from app.repositories.metrics_repository import SqlMetricsRepository

        repo = SqlMetricsRepository()
        fake_db = MagicMock()
        out = repo.get_many(fake_db, [])
        assert out == {}
        fake_db.execute.assert_not_called()
