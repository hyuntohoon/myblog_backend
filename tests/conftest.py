import os

import pytest

# Test env, set at conftest MODULE level — i.e. before pytest collects any test
# module. `app/core/config.py` builds a module-level `settings = get_settings()`
# singleton on first import, and 6 app modules bind to it via
# `from app.core.config import settings`. In the FULL suite, an earlier test
# module's collection-time import would build that singleton from the *empty*
# real env (no GITHUB_TOKEN) → the publish path then 500s → the long-standing 13
# `test_publish`/`test_unpublish` failures that only appeared in the full suite
# (they passed in isolation). Seeding the env here guarantees the first import —
# whenever it happens — sees the test config. `setdefault` so a real env (CI) is
# not clobbered; the autouse fixture below still applies per-test isolation.
_TEST_ENV = {
    "ENV": "local",
    "DATABASE_URL": "postgresql+psycopg://test:test@localhost/test",
    "GITHUB_TOKEN": "ghp_test",
    "GITHUB_REPO_OWNER": "testowner",
    "GITHUB_REPO_NAME": "testrepo",
    "GITHUB_REPO_BRANCH": "main",
    "CONTENT_DIR": "content/blog",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)


@pytest.fixture(autouse=True)
def local_env(monkeypatch):
    """Run all tests in local ENV so edge_guard and JWT checks are bypassed."""
    for _k, _v in _TEST_ENV.items():
        monkeypatch.setenv(_k, _v)


@pytest.fixture
def app():
    import app.core.config as cfg
    cfg.get_settings.cache_clear()
    from app.main import app as fastapi_app
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()
    cfg.get_settings.cache_clear()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c
