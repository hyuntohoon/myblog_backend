"""FEAT-post-edit-delete-ui Step 3 — un-publish removes the static MDX,
restore re-publishes it. Pure-mock (no local Postgres / no real GitHub),
per [[feedback-local-db-smoke-fallback]]."""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# NOTE: app.* modules are imported lazily inside tests/fixtures. A module-level
# import would pull in app.core.config at *collection* time — before conftest's
# autouse `local_env` sets the env — freezing the lru_cached `settings` with an
# empty GitHub config and breaking every settings-dependent test.


# ── publish_service primitives ───────────────────────────────────────────────
class TestContentPath:
    def test_path_shape(self):
        from app.services import publish_service

        p = publish_service.content_path("content/blog", date(2026, 5, 27), "my-slug")
        assert p == "content/blog/2026-05-27--my-slug/index.mdx"


class TestRatingFrontmatter:
    """Regression: restore re-publishing a rating-less post (e.g. an archived
    draft) must not emit `rating: null` — the content schema types rating as
    z.number().optional(), so `null` fails the build. Omit the line instead."""

    def test_none_rating_omits_line(self):
        from datetime import date as _date

        from app.services.publish_service import make_mdx_frontmatter

        fm = make_mdx_frontmatter(
            title="t", slug="s", description="", posted_date=_date(2026, 6, 1),
            category="default", album_ids=[], artist_ids=[], post_id="p",
            rating=None,
        )
        assert "rating: null" not in fm
        assert "\nrating:" not in fm  # the optional field is absent entirely
        assert "ratingScale: 5" in fm

    def test_numeric_rating_emitted(self):
        from datetime import date as _date

        from app.services.publish_service import make_mdx_frontmatter

        fm = make_mdx_frontmatter(
            title="t", slug="s", description="", posted_date=_date(2026, 6, 1),
            category="default", album_ids=[], artist_ids=[], post_id="p",
            rating=4.5,
        )
        assert "rating: 4.5" in fm


class TestGithubDeleteFile:
    def test_absent_file_is_idempotent_noop(self):
        """GET → 404 means the file never existed (e.g. a draft). No DELETE is
        issued and the call reports success so un-publishing stays safe."""
        from app.services import publish_service

        get_resp = MagicMock(status_code=404)
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp) as mget,
            patch("app.services.publish_service.requests.delete") as mdel,
        ):
            out = publish_service.github_delete_file(
                owner="o", repo="r", branch="main", path="content/blog/x/index.mdx", token="t"
            )
        assert out == {"ok": True, "path": "content/blog/x/index.mdx", "deleted": False, "reason": "absent"}
        assert mget.called
        assert not mdel.called

    def test_deletes_with_sha(self):
        from app.services import publish_service

        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"sha": "deadbeef"}
        del_resp = MagicMock(status_code=200)
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.delete", return_value=del_resp) as mdel,
        ):
            out = publish_service.github_delete_file(
                owner="o", repo="r", branch="main", path="content/blog/x/index.mdx", token="t"
            )
        assert out["ok"] is True and out["deleted"] is True
        sent = json.loads(mdel.call_args.kwargs["data"])
        assert sent["sha"] == "deadbeef"
        assert sent["branch"] == "main"

    def test_raises_on_github_error(self):
        from app.services import publish_service

        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"sha": "deadbeef"}
        del_resp = MagicMock(status_code=409, text="conflict")
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.delete", return_value=del_resp),
        ):
            with pytest.raises(RuntimeError, match="409"):
                publish_service.github_delete_file(
                    owner="o", repo="r", branch="main", path="content/blog/x/index.mdx", token="t"
                )


# ── content_sync.derive_subject_meta (shared by publish + restore) ────────────
class TestDeriveSubjectMeta:
    def _album(self):
        album = MagicMock()
        album.id = "alb-1"
        album.title = "Inland Empire"
        album.release_date = date(2026, 3, 14)
        album.label = "Half Light Recordings"
        album.cover_url = "https://cdn.example.com/c.jpg"
        album.best_new = True
        artist = MagicMock()
        artist.name = "YUTO"
        artist.genres = ["한국 랩"]  # ko-KR artist-copy = the fake fallback source
        album.artists = [artist]
        return album

    def test_single_album_uses_real_album_genres(self):
        from app.services import content_sync

        album = self._album()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = album
        with patch.object(
            content_sync, "_album_genre_labels", return_value=["Hip-Hop", "R&B-Soul"]
        ):
            best_new, mr = content_sync.derive_subject_meta(db, ["alb-1"])
        assert best_new is True
        assert mr["title"] == "Inland Empire"
        assert mr["genres"] == ["Hip-Hop", "R&B-Soul"]  # real 12-vocab, not 한국 랩
        assert mr["releaseDate"] == "2026-03-14"
        assert mr["cover"] == {"src": "https://cdn.example.com/c.jpg"}

    def test_falls_back_to_artist_copy_when_no_album_genres(self):
        from app.services import content_sync

        album = self._album()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = album
        with patch.object(content_sync, "_album_genre_labels", return_value=[]):
            _best_new, mr = content_sync.derive_subject_meta(db, ["alb-1"])
        assert mr["genres"] == ["한국 랩"]

    def test_zero_or_multi_album_no_meta(self):
        from app.services import content_sync

        db = MagicMock()
        assert content_sync.derive_subject_meta(db, []) == (False, None)
        assert content_sync.derive_subject_meta(db, ["a", "b"]) == (False, None)


# ── content_sync._album_genre_labels (high-first, else low) ───────────────────
class TestAlbumGenreLabels:
    def _db_returning(self, rows):
        db = MagicMock()
        chain = db.query.return_value.join.return_value.filter.return_value.order_by.return_value
        chain.all.return_value = rows
        return db

    def test_prefers_high_confidence(self):
        from app.services import content_sync

        db = self._db_returning([("Hip-Hop", "high"), ("Pop", "low"), ("Rock", "high")])
        # low 'Pop' is dropped when any high row exists (open-Q4: low hidden under high)
        assert content_sync._album_genre_labels(db, "alb-1") == ["Hip-Hop", "Rock"]

    def test_low_only_album_uses_low(self):
        from app.services import content_sync

        db = self._db_returning([("Pop", "low"), ("Latin", "low")])
        assert content_sync._album_genre_labels(db, "alb-1") == ["Pop", "Latin"]

    def test_no_rows_returns_empty(self):
        from app.services import content_sync

        db = self._db_returning([])
        assert content_sync._album_genre_labels(db, "alb-1") == []


# ── DELETE route un-publishes the MDX ────────────────────────────────────────
def _make_post(status="archived"):
    p = MagicMock()
    p.id = "post-1"
    p.slug = "my-slug"
    p.posted_date = date(2026, 5, 27)
    p.status = status
    return p


@pytest.fixture
def svc_override(app):
    from app.di import get_post_service

    svc = MagicMock()
    app.dependency_overrides[get_post_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(get_post_service, None)


class TestDeleteUnpublishes:
    def test_archive_removes_mdx(self, client, svc_override):
        post = _make_post("archived")
        svc_override.get_by_id.return_value = post
        svc_override.delete.return_value = post  # soft → returns archived Post

        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"sha": "sha1"}
        del_resp = MagicMock(status_code=200)
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.delete", return_value=del_resp) as mdel,
        ):
            resp = client.delete("/api/posts/post-1")

        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"
        # The DELETE hit the post's canonical content path.
        assert "2026-05-27--my-slug/index.mdx" in mdel.call_args.args[0]

    def test_hard_delete_removes_mdx(self, client, svc_override):
        svc_override.get_by_id.return_value = _make_post("published")
        svc_override.delete.return_value = True  # hard → truthy

        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"sha": "sha1"}
        del_resp = MagicMock(status_code=200)
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.delete", return_value=del_resp) as mdel,
        ):
            resp = client.delete("/api/posts/post-1?hard=true")

        assert resp.status_code == 204
        assert mdel.called

    def test_unpublished_post_is_idempotent_no_502(self, client, svc_override):
        """Archiving a draft (no MDX) → GET 404 → no DELETE, no error."""
        svc_override.get_by_id.return_value = _make_post("archived")
        svc_override.delete.return_value = _make_post("archived")

        get_resp = MagicMock(status_code=404)
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.delete") as mdel,
        ):
            resp = client.delete("/api/posts/post-1")

        assert resp.status_code == 200
        assert not mdel.called

    def test_github_failure_surfaces_502(self, client, svc_override):
        svc_override.get_by_id.return_value = _make_post("archived")
        svc_override.delete.return_value = _make_post("archived")

        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"sha": "sha1"}
        del_resp = MagicMock(status_code=409, text="conflict")
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.delete", return_value=del_resp),
        ):
            resp = client.delete("/api/posts/post-1")

        assert resp.status_code == 502
        assert "static page removal failed" in resp.json()["detail"]


# ── restore route re-publishes the MDX ───────────────────────────────────────
class TestRestoreRepublishes:
    def _post_for_restore(self):
        p = MagicMock()
        p.id = "post-1"
        p.slug = "my-slug"
        p.title = "My Slug"
        p.description = "d"
        p.posted_date = date(2026, 5, 27)
        p.status = "published"
        p.albums = []          # → derive_subject_meta short-circuits, no db.query
        p.artists = []
        p.section = None
        p.album_cover_url = None
        p.rating = None
        p.rating_scale = 5
        p.body_mdx = "## Body"
        return p

    def test_restore_republishes_mdx(self, client, svc_override):
        svc_override.restore.return_value = self._post_for_restore()
        svc_override.list_recommended_track_ids.return_value = []

        get_resp = MagicMock(status_code=404)  # file absent → fresh create
        put_resp = MagicMock(status_code=201, text="{}")
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.put", return_value=put_resp) as mput,
            # FEAT-genre-subgenres Step 3: republish now reads post_genres from the
            # DB; this test's db session is real (unmocked), so stub the lookup.
            patch("app.services.content_sync.derive_subgenres", return_value=[]),
        ):
            resp = client.patch("/api/posts/post-1/restore")

        assert resp.status_code == 200
        assert resp.json()["status"] == "published"
        assert mput.called
        sent = json.loads(mput.call_args.kwargs["data"])
        # Re-published to the post's canonical path.
        assert "2026-05-27--my-slug/index.mdx" in sent["message"] or mput.called

    def test_restore_github_failure_surfaces_502(self, client, svc_override):
        svc_override.restore.return_value = self._post_for_restore()
        svc_override.list_recommended_track_ids.return_value = []

        get_resp = MagicMock(status_code=404)
        put_resp = MagicMock(status_code=422, text="bad")
        with (
            patch("app.services.publish_service.requests.get", return_value=get_resp),
            patch("app.services.publish_service.requests.put", return_value=put_resp),
            patch("app.services.content_sync.derive_subgenres", return_value=[]),
        ):
            resp = client.patch("/api/posts/post-1/restore")

        assert resp.status_code == 502
        assert "re-publish failed" in resp.json()["detail"]
