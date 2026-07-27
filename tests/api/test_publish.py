from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _stub_get_db(app):
    """FEAT-writer-lowfreq-redesign Step 5: the publish route now reads
    albums.best_new from DB to seed frontmatter. Stub get_db so tests stay
    pure (no local Postgres required, per [[feedback-local-db-smoke-fallback]]).
    The album lookup returns None → best_new=False, which matches the
    pre-Step-5 frontmatter exactly.
    """
    from app.db.session import get_db

    stub_session = MagicMock()
    stub_session.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[get_db] = lambda: stub_session
    yield


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

    def test_tags_written_to_frontmatter(self, client):
        # STAB-5: review tags must land in the MDX frontmatter so the static
        # /reviews page can filter + render them at build time.
        mock_get, mock_put = _mock_github(201)
        payload = {**VALID_PAYLOAD, "tags": ["album review", "reissue"]}
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=payload)

        assert resp.status_code == 200
        import base64
        import json
        sent = json.loads(mock_put.call_args.kwargs["data"])
        body = base64.b64decode(sent["content"]).decode("utf-8")
        line = next(ln for ln in body.splitlines() if ln.startswith("tags:"))
        assert json.loads(line.split("tags:", 1)[1].strip()) == ["album review", "reissue"]

    def test_no_tags_emits_empty_list(self, client):
        # Omitted tags ⇒ `tags: []` (parses cleanly; /reviews sees no tags).
        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        import base64
        import json
        sent = json.loads(mock_put.call_args.kwargs["data"])
        body = base64.b64decode(sent["content"]).decode("utf-8")
        assert "tags: []" in body

    def test_subgenres_written_to_frontmatter(self, client):
        # FEAT-genre-subgenres Step 3: editorial sub-genre tags (derived from
        # post_genres by post_id) land in the MDX frontmatter so the review page
        # can render the critic's precise genre call.
        mock_get, mock_put = _mock_github(201)
        subs = [{"slug": "boom-bap", "label": "Boom Bap"}, {"slug": "jazz-rap", "label": "Jazz Rap"}]
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
            patch("app.api.routes.publish.derive_subgenres", return_value=subs),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        import base64
        import json
        sent = json.loads(mock_put.call_args.kwargs["data"])
        body = base64.b64decode(sent["content"]).decode("utf-8")
        line = next(ln for ln in body.splitlines() if ln.startswith("subgenres:"))
        assert json.loads(line.split("subgenres:", 1)[1].strip()) == subs

    def test_no_subgenres_emits_empty_list(self, client):
        # No tagged genres ⇒ `subgenres: []` (parses cleanly; review page hides
        # the block). The autouse stub session yields no post_genres rows.
        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        import base64
        import json
        sent = json.loads(mock_put.call_args.kwargs["data"])
        body = base64.b64decode(sent["content"]).decode("utf-8")
        assert "subgenres: []" in body

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


class TestFrontmatterYaml:
    """Audit 2026-07-26 A-2: frontmatter used to be written with Python repr(),
    which emits backslash-escaped single quotes for a value containing both
    `'` and `"` — invalid YAML. Publish returned 200, then every Astro build
    failed at getCollection until the file was found by hand. These tests
    parse the emitted frontmatter with a real YAML parser (what the build
    does), not by substring."""

    EVIL_TITLE = 'Taylor Swift\'s "1989"'

    @staticmethod
    def _parse_frontmatter(mdx: str) -> dict:
        import yaml

        # Same extraction the Astro build does: the block between the first
        # two `---` lines.
        inner = mdx.split("---\n", 2)[1]
        return yaml.safe_load(inner)

    def test_old_repr_form_is_invalid_yaml(self):
        # Mutation check: prove the parser these tests rely on would have
        # caught the original defect — repr() of the evil title must be
        # rejected, so a regression back to repr() fails loudly here.
        import yaml

        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(f"title: {self.EVIL_TITLE!r}")

    def test_both_quote_kinds_roundtrip(self):
        from datetime import date

        from app.services.publish_service import make_mdx_frontmatter

        out = make_mdx_frontmatter(
            title=self.EVIL_TITLE,
            slug="taylor-swift-s-1989",
            description='she said "it\'s fine"',
            posted_date=date(2026, 7, 28),
            category="music",
            album_ids=["a1"],
            artist_ids=["r1"],
            post_id="p-1",
        )
        fm = self._parse_frontmatter(out)
        assert fm["title"] == self.EVIL_TITLE
        assert fm["description"] == 'she said "it\'s fine"'
        assert fm["slug"] == "taylor-swift-s-1989"

    def test_backslash_newline_and_korean_roundtrip(self):
        # repr() also mangled backslashes silently (YAML single-quoted keeps
        # `\\` literal); JSON-quoting must roundtrip them, plus multi-line
        # descriptions and non-ASCII intact.
        from datetime import date

        from app.services.publish_service import make_mdx_frontmatter

        out = make_mdx_frontmatter(
            title="열기 \\ 続き",
            slug="s",
            description="line one\nline two",
            posted_date=date(2026, 7, 28),
            category="음악",
            album_ids=[],
            artist_ids=[],
            post_id="p-2",
        )
        fm = self._parse_frontmatter(out)
        assert fm["title"] == "열기 \\ 続き"
        assert fm["description"] == "line one\nline two"
        assert fm["category"] == "음악"

    def test_publish_route_emits_parseable_frontmatter(self, client):
        # End-to-end through POST /api/publish: the exact A-2 payload must
        # produce frontmatter the build can parse, with the title intact.
        mock_get, mock_put = _mock_github(201)
        payload = {**VALID_PAYLOAD, "title": self.EVIL_TITLE, "slug": "evil"}
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=payload)

        assert resp.status_code == 200
        import base64
        import json
        sent = json.loads(mock_put.call_args.kwargs["data"])
        body = base64.b64decode(sent["content"]).decode("utf-8")
        fm = self._parse_frontmatter(body)
        assert fm["title"] == self.EVIL_TITLE


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


class TestMusicReviewFrontmatter:
    """FEAT-view-redesign Step 5 follow-up: publish writes a musicReview
    block to MDX so the read-page hero (.lfq-hero) can show
    label / year / artist / album-title / genres without a runtime fetch."""

    def test_musicReview_serialized_when_subject_album_present(self, app, client):
        from app.db.session import get_db
        from datetime import date

        album = MagicMock()
        album.id = "alb-1"
        album.title = "Inland Empire"
        album.release_date = date(2026, 3, 14)
        album.label = "Half Light Recordings"
        album.cover_url = "https://cdn.example.com/cover.jpg"
        album.best_new = True
        artist = MagicMock()
        artist.name = "유토 / YUTO"
        artist.genres = ["Electronic", "Ambient"]
        album.artists = [artist]

        stub_session = MagicMock()
        stub_session.query.return_value.filter.return_value.first.return_value = album
        app.dependency_overrides[get_db] = lambda: stub_session

        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        # The frontmatter is base64-encoded inside the PUT payload body.
        import base64
        import json
        sent = json.loads(mock_put.call_args.kwargs["data"])
        body = base64.b64decode(sent["content"]).decode("utf-8")
        assert "musicReview: " in body
        # The line is a single JSON object (YAML-compatible flow style).
        line = next(ln for ln in body.splitlines() if ln.startswith("musicReview:"))
        mr = json.loads(line.split("musicReview:", 1)[1].strip())
        assert mr["title"] == "Inland Empire"
        assert mr["label"] == "Half Light Recordings"
        assert mr["artists"] == ["유토 / YUTO"]
        assert mr["genres"] == ["Electronic", "Ambient"]
        assert mr["releaseDate"] == "2026-03-14"
        assert mr["cover"] == {"src": "https://cdn.example.com/cover.jpg"}
        assert "bestNew: true" in body  # unchanged seed path
        app.dependency_overrides.clear()

    def test_no_musicReview_when_album_not_found(self, client):
        """Existing autouse `_stub_get_db` returns None for the album lookup.
        Publish must still succeed; frontmatter just omits `musicReview:`."""
        mock_get, mock_put = _mock_github(201)
        with (
            patch("app.services.publish_service.requests.get", mock_get),
            patch("app.services.publish_service.requests.put", mock_put),
        ):
            resp = client.post("/api/publish", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        import base64
        import json
        sent = json.loads(mock_put.call_args.kwargs["data"])
        body = base64.b64decode(sent["content"]).decode("utf-8")
        assert "musicReview:" not in body


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
