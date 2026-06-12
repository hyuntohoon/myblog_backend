from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from app.di import get_bucket_service
from app.services.bucket_service import (
    AlbumNotFoundError,
    BucketNotFoundError,
    DuplicateItemError,
    ItemNotFoundError,
)


def _album(album_id="alb-1", title="Album", artists=("Artist A",)):
    a = MagicMock()
    a.id = album_id
    a.title = title
    a.cover_url = "https://cdn/cover.jpg"
    a.release_date = date(2026, 5, 1)
    a.popularity = 70
    a.artists = [MagicMock(name=n) for n in artists]
    # MagicMock(name=...) sets the mock's repr name, not a `.name` attr — set it.
    for m, n in zip(a.artists, artists):
        m.name = n
    return a


def _item(item_id="it-1", album_id="alb-1", position=0, status="candidate"):
    it = MagicMock()
    it.id = item_id
    it.album_id = album_id
    it.position = position
    it.note = None
    it.status = status
    it.post_id = None
    it.rec_reason = "신보"
    # research_selected is a validated bool on BucketItemResponse — a bare MagicMock
    # would fail validation, so set it explicitly (same reason as bucket.kind below).
    it.research_selected = False
    it.album = _album(album_id=album_id)
    return it


def _bucket(
    bucket_id="bk-1", name="꼭", position=0, is_done=False, items=(),
    children=(), kind="review",
):
    b = MagicMock()
    b.id = bucket_id
    b.name = name
    b.position = position
    b.color = None
    b.is_done = is_done
    # FEAT-spotify-library-sync: BucketResponse now carries `kind` (validated as a
    # str), so set it explicitly — a bare MagicMock auto-vivifies a non-string here.
    b.kind = kind
    # research_mode is a validated str on BucketResponse (off|all|selected) — set it
    # explicitly so a bare MagicMock doesn't auto-vivify a non-string / truthy value.
    b.research_mode = "off"
    b.items = list(items)
    # list_buckets() attaches descendants on the transient `children_nodes`
    # attribute; the route serializes it recursively. MagicMock would otherwise
    # auto-vivify a truthy mock here, so set it explicitly.
    b.children_nodes = list(children)
    return b


def _override(app, svc, research_svc=None):
    # Import get_db lazily: a module-top import would pull app.core.config at
    # pytest collection time (before the autouse local_env fixture sets env),
    # caching an empty settings singleton that breaks content_sync in other
    # test modules.
    from app.db.session import get_db
    from app.di import get_research_service

    app.dependency_overrides[get_bucket_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()
    # The bucket reads/mutations now batch research status (cover-badge seed). The
    # real ResearchService would run a query against the MagicMock db, so override
    # it; default returns an empty map → research_status=None on every item.
    if research_svc is None:
        research_svc = MagicMock()
        research_svc.status_map.return_value = {}
    app.dependency_overrides[get_research_service] = lambda: research_svc


class TestListBuckets:
    def test_list_returns_buckets_with_items(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item()])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["buckets"]) == 1
        bucket = data["buckets"][0]
        assert bucket["name"] == "꼭"
        assert bucket["items"][0]["album"]["title"] == "Album"
        assert bucket["items"][0]["already_reviewed"] is False
        assert bucket["items"][0]["rec_reason"] == "신보"
        app.dependency_overrides.clear()

    def test_already_reviewed_badge_set_from_post_albums(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item(album_id="alb-rev")])]
        svc.reviewed_album_ids.return_value = {"alb-rev"}
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        assert resp.json()["buckets"][0]["items"][0]["already_reviewed"] is True
        app.dependency_overrides.clear()

    def test_research_status_defaults_null_when_never_researched(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item()])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)  # default research_svc → empty status map

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        assert resp.json()["buckets"][0]["items"][0]["research_status"] is None
        app.dependency_overrides.clear()

    def test_research_status_seeded_from_status_map(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item(album_id="alb-done")])]
        svc.reviewed_album_ids.return_value = set()
        research_svc = MagicMock()
        research_svc.status_map.return_value = {"alb-done": "done"}
        _override(app, svc, research_svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        item = resp.json()["buckets"][0]["items"][0]
        # The cover badge can now paint the done dot on first paint (no per-cover GET).
        assert item["research_status"] == "done"
        research_svc.status_map.assert_called_once()
        app.dependency_overrides.clear()


class TestCreateBucket:
    def test_create_returns_201(self, client, app):
        svc = MagicMock()
        svc.create_bucket.return_value = _bucket(name="신보")
        _override(app, svc)

        resp = client.post("/api/buckets", json={"name": "신보"})

        assert resp.status_code == 201
        assert resp.json()["name"] == "신보"
        assert resp.json()["items"] == []
        app.dependency_overrides.clear()

    def test_blank_name_returns_400(self, client, app):
        # Whitespace passes pydantic min_length=1 but the service rejects it.
        svc = MagicMock()
        svc.create_bucket.side_effect = ValueError("name required")
        _override(app, svc)

        resp = client.post("/api/buckets", json={"name": " "})

        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_empty_name_rejected_by_schema_422(self, client, app):
        _override(app, MagicMock())
        resp = client.post("/api/buckets", json={"name": ""})
        assert resp.status_code == 422
        app.dependency_overrides.clear()

    def test_create_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.post("/api/buckets", json={"name": "x"})

        assert resp.status_code == 401


class TestUpdateBucket:
    def test_update_not_found_returns_404(self, client, app):
        svc = MagicMock()
        svc.update_bucket.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-x", json={"name": "new"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_second_done_bucket_returns_409(self, client, app):
        from sqlalchemy.exc import IntegrityError

        svc = MagicMock()
        svc.update_bucket.side_effect = IntegrityError("stmt", {}, Exception("dup"))
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1", json={"is_done": True})

        assert resp.status_code == 409
        app.dependency_overrides.clear()

    def test_update_forwards_only_set_fields(self, client, app):
        svc = MagicMock()
        svc.update_bucket.return_value = _bucket(name="renamed")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1", json={"name": "renamed"})

        assert resp.status_code == 200
        kwargs = svc.update_bucket.call_args.kwargs
        assert kwargs == {"name": "renamed"}
        app.dependency_overrides.clear()

    def test_update_color_null_forwards_explicit_none(self, client, app):
        # Resetting a bucket to the default ink sends {"color": null}. exclude_unset
        # must keep the field so color=None reaches the service as an explicit clear
        # (not stripped like an omitted field). Regression: the service ignored the
        # clear and the color could be set but never reset.
        svc = MagicMock()
        svc.update_bucket.return_value = _bucket(name="꼭")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1", json={"color": None})

        assert resp.status_code == 200
        kwargs = svc.update_bucket.call_args.kwargs
        assert kwargs == {"color": None}
        app.dependency_overrides.clear()


class TestDeleteBucket:
    def test_delete_returns_204(self, client, app):
        svc = MagicMock()
        svc.delete_bucket.return_value = True
        _override(app, svc)

        resp = client.delete("/api/buckets/bk-1")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_nonexistent_returns_404(self, client, app):
        svc = MagicMock()
        svc.delete_bucket.return_value = False
        _override(app, svc)

        resp = client.delete("/api/buckets/no-such")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestAddItem:
    def test_add_returns_201_with_item(self, client, app):
        svc = MagicMock()
        svc.add_item.return_value = _item()
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-1"})

        assert resp.status_code == 201
        body = resp.json()
        assert body["album_id"] == "alb-1"
        assert body["already_reviewed"] is False
        app.dependency_overrides.clear()

    def test_add_already_reviewed_album_sets_badge(self, client, app):
        svc = MagicMock()
        svc.add_item.return_value = _item(album_id="alb-rev")
        svc.reviewed_album_ids.return_value = {"alb-rev"}
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-rev"})

        assert resp.status_code == 201
        assert resp.json()["already_reviewed"] is True
        app.dependency_overrides.clear()

    def test_add_to_missing_bucket_returns_404(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-x/items", json={"album_id": "alb-1"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_add_missing_album_returns_404(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = AlbumNotFoundError("alb-x")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-x"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_duplicate_album_in_bucket_returns_409(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = DuplicateItemError("alb-1")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-1"})

        assert resp.status_code == 409
        app.dependency_overrides.clear()


class TestUpdateItem:
    def test_update_status_returns_200(self, client, app):
        svc = MagicMock()
        svc.update_item.return_value = _item(status="published")
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.patch(
            "/api/buckets/bk-1/items/it-1", json={"status": "published"}
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "published"
        kwargs = svc.update_item.call_args.kwargs
        assert kwargs == {"status": "published"}
        app.dependency_overrides.clear()

    def test_update_missing_item_returns_404(self, client, app):
        svc = MagicMock()
        svc.update_item.side_effect = ItemNotFoundError("it-x")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1/items/it-x", json={"note": "n"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_update_item_bad_status_returns_422(self, client, app):
        _override(app, MagicMock())
        resp = client.patch(
            "/api/buckets/bk-1/items/it-1", json={"status": "bogus"}
        )
        assert resp.status_code == 422
        app.dependency_overrides.clear()


class TestDeleteItem:
    def test_delete_returns_204(self, client, app):
        svc = MagicMock()
        svc.delete_item.return_value = True
        _override(app, svc)

        resp = client.delete("/api/buckets/bk-1/items/it-1")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_missing_returns_404(self, client, app):
        svc = MagicMock()
        svc.delete_item.return_value = False
        _override(app, svc)

        resp = client.delete("/api/buckets/bk-1/items/no-such")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestReorder:
    def test_reorder_returns_204(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-1", "item_ids": ["it-2", "it-1"]}]},
        )

        assert resp.status_code == 204
        payload = svc.reorder.call_args.args[1]
        assert payload == [{"id": "bk-1", "item_ids": ["it-2", "it-1"]}]
        app.dependency_overrides.clear()

    def test_reorder_into_all_mode_bucket_enqueues_research(self, client, app):
        # An album dragged into an 'all'-mode bucket persists via /reorder (not
        # add_item), so the reorder path must enqueue the now-resident items.
        svc = MagicMock()
        all_bucket = _bucket(bucket_id="bk-all")
        all_bucket.research_mode = "all"
        svc.get_bucket.return_value = all_bucket
        research_svc = MagicMock()
        research_svc.enqueue_bucket.return_value = 1
        _override(app, svc, research_svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-all", "item_ids": ["it-1"]}]},
        )

        assert resp.status_code == 204
        research_svc.enqueue_bucket.assert_called_once()
        app.dependency_overrides.clear()

    def test_reorder_into_off_mode_bucket_skips_research(self, client, app):
        svc = MagicMock()
        svc.get_bucket.return_value = _bucket(bucket_id="bk-off")  # research_mode='off'
        research_svc = MagicMock()
        _override(app, svc, research_svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-off", "item_ids": ["it-1"]}]},
        )

        assert resp.status_code == 204
        research_svc.enqueue_bucket.assert_not_called()
        app.dependency_overrides.clear()

    def test_reorder_unknown_bucket_returns_404(self, client, app):
        svc = MagicMock()
        svc.reorder.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-x", "item_ids": []}]},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_reorder_unknown_item_returns_404(self, client, app):
        svc = MagicMock()
        svc.reorder.side_effect = ItemNotFoundError("it-x")
        _override(app, svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-1", "item_ids": ["it-x"]}]},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestNestedTree:
    def test_child_nests_under_parent_not_at_top_level(self, client, app):
        # list_buckets() returns only roots; the child rides on the root's
        # children_nodes. The serialized response must place the child under
        # parent.children — never as a second top-level bucket.
        child = _bucket(bucket_id="bk-child", name="child", items=[_item()])
        root = _bucket(bucket_id="bk-root", name="root", children=[child])
        svc = MagicMock()
        svc.list_buckets.return_value = [root]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        data = resp.json()
        # Exactly one top-level bucket (the root).
        assert [b["id"] for b in data["buckets"]] == ["bk-root"]
        top = data["buckets"][0]
        assert [c["id"] for c in top["children"]] == ["bk-child"]
        # The child's item is reachable through the nested children, and the
        # already_reviewed batch covered tree items.
        assert top["children"][0]["items"][0]["album"]["title"] == "Album"
        app.dependency_overrides.clear()

    def test_root_with_no_children_serializes_empty_list(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item()])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        assert resp.json()["buckets"][0]["children"] == []
        app.dependency_overrides.clear()


class TestMoveBucket:
    def test_move_reparent_success_returns_tree(self, client, app):
        svc = MagicMock()
        # move_bucket succeeds (return value unused by route); the route then
        # re-reads the nested tree via list_buckets.
        child = _bucket(bucket_id="bk-2", name="child")
        svc.list_buckets.return_value = [_bucket(bucket_id="bk-1", children=[child])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-2/move", json={"parent_id": "bk-1", "position": 0}
        )

        assert resp.status_code == 200
        # Route forwarded parent_id + position to the service.
        kwargs = svc.move_bucket.call_args.kwargs
        assert kwargs == {"parent_id": "bk-1", "position": 0}
        # Returns the full nested tree.
        data = resp.json()
        assert [b["id"] for b in data["buckets"]] == ["bk-1"]
        assert [c["id"] for c in data["buckets"][0]["children"]] == ["bk-2"]
        app.dependency_overrides.clear()

    def test_move_to_root_parent_null(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(bucket_id="bk-2")]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-2/move", json={"parent_id": None, "position": 1}
        )

        assert resp.status_code == 200
        kwargs = svc.move_bucket.call_args.kwargs
        assert kwargs == {"parent_id": None, "position": 1}
        app.dependency_overrides.clear()

    def test_move_cycle_returns_400(self, client, app):
        svc = MagicMock()
        svc.move_bucket.side_effect = ValueError(
            "cannot move a bucket under its own descendant"
        )
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-1/move", json={"parent_id": "bk-2", "position": 0}
        )

        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_move_missing_bucket_returns_404(self, client, app):
        svc = MagicMock()
        svc.move_bucket.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-x/move", json={"parent_id": None, "position": 0}
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_move_missing_position_returns_422(self, client, app):
        _override(app, MagicMock())
        resp = client.put("/api/buckets/bk-1/move", json={"parent_id": None})
        assert resp.status_code == 422
        app.dependency_overrides.clear()
