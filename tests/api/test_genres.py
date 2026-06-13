from __future__ import annotations

from unittest.mock import MagicMock

from app.di import get_genre_service
from app.services.genre_service import (
    DuplicateSlugError,
    GenreNotFoundError,
    ParentNotFoundError,
)


def _genre(
    genre_id="gen-1", slug="pop", label="Pop", parent_id=None,
    definition_md="", position=0, album_count=0, children=(),
):
    g = MagicMock()
    g.id = genre_id
    g.slug = slug
    g.label = label
    g.parent_id = parent_id
    g.definition_md = definition_md
    g.position = position
    # Transient count attached by list_tree(); the route serializes it (a bare
    # MagicMock would be truthy-but-non-int and break int() in _node).
    g.album_count = album_count
    # list_tree() attaches descendants on the transient `children_nodes`; the route
    # serializes it recursively. A bare MagicMock would auto-vivify a truthy child.
    g.children_nodes = list(children)
    return g


def _override(app, svc):
    # Import get_db lazily so a module-top import doesn't pull app.core.config at
    # collection time (empty settings singleton — reference-backend-test-config-import).
    from app.db.session import get_db

    app.dependency_overrides[get_genre_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


class TestGenreTree:
    def test_tree_returns_nested_nodes(self, client, app):
        child = _genre(genre_id="gen-2", slug="city-pop", label="City Pop", parent_id="gen-1")
        root = _genre(children=[child])
        svc = MagicMock()
        svc.list_tree.return_value = [root]
        _override(app, svc)

        resp = client.get("/api/genres/tree")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["genres"]) == 1
        node = data["genres"][0]
        assert node["slug"] == "pop"
        assert node["label"] == "Pop"
        assert node["parent_id"] is None
        assert len(node["children"]) == 1
        assert node["children"][0]["slug"] == "city-pop"
        assert node["children"][0]["parent_id"] == "gen-1"
        app.dependency_overrides.clear()

    def test_tree_serializes_album_count(self, client, app):
        # album_count (transient, set by list_tree) flows into the response and
        # drives the /genres share-bars. Defaults to 0 when a genre has no albums.
        root = _genre(album_count=1210, children=[_genre(genre_id="gen-2", slug="drill", label="Drill")])
        svc = MagicMock()
        svc.list_tree.return_value = [root]
        _override(app, svc)

        resp = client.get("/api/genres/tree")

        assert resp.status_code == 200
        node = resp.json()["genres"][0]
        assert node["album_count"] == 1210
        assert node["children"][0]["album_count"] == 0
        app.dependency_overrides.clear()

    def test_tree_empty(self, client, app):
        svc = MagicMock()
        svc.list_tree.return_value = []
        _override(app, svc)

        resp = client.get("/api/genres/tree")

        assert resp.status_code == 200
        assert resp.json() == {"genres": []}
        app.dependency_overrides.clear()


class TestCreateGenre:
    def test_create_returns_201(self, client, app):
        svc = MagicMock()
        svc.create.return_value = _genre(slug="latin", label="Latin")
        _override(app, svc)

        resp = client.post("/api/genres", json={"slug": "latin", "label": "Latin"})

        assert resp.status_code == 201
        assert resp.json()["slug"] == "latin"
        assert resp.json()["children"] == []
        svc.create.assert_called_once()
        app.dependency_overrides.clear()

    def test_create_duplicate_slug_409(self, client, app):
        svc = MagicMock()
        svc.create.side_effect = DuplicateSlugError("pop")
        _override(app, svc)

        resp = client.post("/api/genres", json={"slug": "pop", "label": "Pop"})

        assert resp.status_code == 409
        app.dependency_overrides.clear()

    def test_create_missing_parent_400(self, client, app):
        svc = MagicMock()
        svc.create.side_effect = ParentNotFoundError("nope")
        _override(app, svc)

        resp = client.post(
            "/api/genres", json={"slug": "x", "label": "X", "parent_id": "nope"}
        )

        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_create_blank_slug_422(self, client, app):
        # min_length=1 on the schema rejects before the service is touched.
        svc = MagicMock()
        _override(app, svc)

        resp = client.post("/api/genres", json={"slug": "", "label": "X"})

        assert resp.status_code == 422
        svc.create.assert_not_called()
        app.dependency_overrides.clear()


class TestUpdateGenre:
    def test_update_definition_returns_node(self, client, app):
        svc = MagicMock()
        svc.update.return_value = _genre(definition_md="The idol-industry category.")
        _override(app, svc)

        resp = client.put(
            "/api/genres/gen-1", json={"definition_md": "The idol-industry category."}
        )

        assert resp.status_code == 200
        assert resp.json()["definition_md"] == "The idol-industry category."
        # exclude_unset → only the sent field reaches the service.
        _, kwargs = svc.update.call_args
        assert kwargs == {"definition_md": "The idol-industry category."}
        app.dependency_overrides.clear()

    def test_update_missing_genre_404(self, client, app):
        svc = MagicMock()
        svc.update.side_effect = GenreNotFoundError("gen-x")
        _override(app, svc)

        resp = client.put("/api/genres/gen-x", json={"label": "X"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_update_blank_label_400(self, client, app):
        svc = MagicMock()
        svc.update.side_effect = ValueError("label cannot be empty")
        _override(app, svc)

        resp = client.put("/api/genres/gen-1", json={"label": "   "})

        assert resp.status_code == 400
        app.dependency_overrides.clear()
