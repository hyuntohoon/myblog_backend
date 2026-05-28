from __future__ import annotations

from unittest.mock import MagicMock, patch


VALID_PAYLOAD = {
    "title": "Test Album Review",
    "body_mdx": "## Review\n\nGreat album.",
    "category": "music",
    "description": "A test review",
    "posted_date": "2026-05-27",
    "album_ids": ["4eLPsYPBmXABThSJ821sqY"],
    "artist_ids": ["0TnOYISbd1XYRBk9myaseg"],
    "post_id": "abc-123",
    "rating": 4.5,
    "rating_scale": 5,
}


def _mock_github(status_code: int = 201):
    """Return (mock_get, mock_put) that simulate a successful GitHub API call."""
    get_resp = MagicMock()
    get_resp.status_code = 404  # file does not exist yet

    put_resp = MagicMock()
    put_resp.status_code = status_code
    put_resp.text = "{}"

    mock_get = MagicMock(return_value=get_resp)
    mock_put = MagicMock(return_value=put_resp)
    return mock_get, mock_put


class TestPublishHappyPath:
    def test_create_post_returns_ok(self, client):
        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "test-album-review" in body["slug"]
        assert "index.mdx" in body["path"]
        assert "testowner/testrepo" in body["github_url"]

    def test_slug_auto_generated_from_title(self, client):
        mock_get, mock_put = _mock_github(201)
        payload = {**VALID_PAYLOAD, "title": "My New Post", "slug": None}
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=payload)

        assert resp.status_code == 200
        assert resp.json()["slug"] == "my-new-post"

    def test_explicit_slug_respected(self, client):
        mock_get, mock_put = _mock_github(201)
        payload = {**VALID_PAYLOAD, "slug": "custom-slug"}
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=payload)

        assert resp.status_code == 200
        assert resp.json()["slug"] == "custom-slug"

    def test_update_existing_file_sends_sha(self, client):
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"sha": "existing_sha_abc"}

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.text = "{}"

        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.put", return_value=put_resp) as mock_put,
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        call_kwargs = mock_put.call_args
        import json
        sent_payload = json.loads(call_kwargs[1]["data"])
        assert sent_payload["sha"] == "existing_sha_abc"


class TestPublishAuthorFromCognito:
    def _capture_mdx(self, mock_put):
        """Decode the base64-encoded MDX content sent to GitHub."""
        import base64
        import json as _json
        sent = _json.loads(mock_put.call_args[1]["data"])
        return base64.b64decode(sent["content"]).decode("utf-8")

    def test_author_from_name_claim(self, app, client):
        from app.core.auth import require_cognito_token
        app.dependency_overrides[require_cognito_token] = lambda: {
            "name": "지훈",
            "preferred_username": "hyuntohoon",
            "email": "test@example.com",
        }

        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        mdx = self._capture_mdx(mock_put)
        assert "author: '지훈'" in mdx

    def test_author_falls_back_to_preferred_username(self, app, client):
        from app.core.auth import require_cognito_token
        app.dependency_overrides[require_cognito_token] = lambda: {
            "preferred_username": "hyuntohoon",
            "email": "test@example.com",
        }

        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        mdx = self._capture_mdx(mock_put)
        assert "author: 'hyuntohoon'" in mdx

    def test_no_author_line_when_claims_empty(self, client):
        # ENV=local in conftest → require_cognito_token already returns {}
        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        mdx = self._capture_mdx(mock_put)
        assert "author:" not in mdx


class TestPublishMissingToken:
    def test_missing_github_token_returns_500(self, client):
        import app.core.config as cfg
        cfg.get_settings.cache_clear()

        original = cfg.settings.GITHUB_TOKEN
        cfg.settings.GITHUB_TOKEN = ""
        try:
            resp = client.post("/api/publish", json=VALID_PAYLOAD)
        finally:
            cfg.settings.GITHUB_TOKEN = original
            cfg.get_settings.cache_clear()

        assert resp.status_code == 500
        assert "Missing GitHub" in resp.json()["detail"]


class TestPublishJwtRequired:
    def test_missing_jwt_returns_401_in_prod_env(self, client):
        # settings is bound at import time; patch the auth module's reference directly
        import app.core.auth as auth_module
        from unittest.mock import MagicMock

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 401
