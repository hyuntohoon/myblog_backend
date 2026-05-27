import pytest


@pytest.fixture(autouse=True)
def local_env(monkeypatch):
    """Run all tests in local ENV so edge_guard and JWT checks are bypassed."""
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("GITHUB_REPO_OWNER", "testowner")
    monkeypatch.setenv("GITHUB_REPO_NAME", "testrepo")
    monkeypatch.setenv("GITHUB_REPO_BRANCH", "main")
    monkeypatch.setenv("CONTENT_DIR", "content/blog")


@pytest.fixture
def client():
    # Import after env is set by local_env fixture
    import app.core.config as cfg
    cfg.get_settings.cache_clear()

    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

    cfg.get_settings.cache_clear()
