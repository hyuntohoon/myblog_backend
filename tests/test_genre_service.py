from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.genre_service import (
    GenreNotFoundError,
    GenreService,
    _UNSET,
)


class _Row:
    """Minimal stand-in for a Genre ORM row (no DB / shared_db import needed)."""

    def __init__(self, id, parent_id=None, position=0, label="L",
                 definition_md=""):
        self.id = id
        self.parent_id = parent_id
        self.position = position
        self.label = label
        self.definition_md = definition_md


def _db_returning(rows):
    """A MagicMock db whose execute(...).scalars().all() yields `rows`."""
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = rows
    return db


class TestListTree:
    def test_nests_children_under_parents_and_returns_roots_only(self):
        a = _Row("a")
        b = _Row("b")
        a1 = _Row("a1", parent_id="a")
        a2 = _Row("a2", parent_id="a")
        db = _db_returning([a, a1, a2, b])

        roots = GenreService().list_tree(db)

        assert {r.id for r in roots} == {"a", "b"}
        assert {c.id for c in a.children_nodes} == {"a1", "a2"}
        assert b.children_nodes == []

    def test_empty_catalog_returns_empty(self):
        assert GenreService().list_tree(_db_returning([])) == []


class TestUpdate:
    def test_missing_genre_raises(self):
        svc = GenreService()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(GenreNotFoundError):
            svc.update(db, "nope", label="X")

    def test_blank_label_raises_valueerror(self):
        svc = GenreService()
        row = _Row("a", label="Pop")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row
        with pytest.raises(ValueError, match="label cannot be empty"):
            svc.update(db, "a", label="   ")

    def test_explicit_empty_definition_clears_and_commits(self):
        svc = GenreService()
        row = _Row("a", definition_md="old text")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row

        svc.update(db, "a", definition_md="")

        assert row.definition_md == ""
        db.commit.assert_called_once()

    def test_unset_definition_leaves_value_untouched(self):
        svc = GenreService()
        row = _Row("a", definition_md="keep me")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row

        # _UNSET is the sentinel the route passes when definition_md is not sent.
        svc.update(db, "a", definition_md=_UNSET)

        assert row.definition_md == "keep me"
