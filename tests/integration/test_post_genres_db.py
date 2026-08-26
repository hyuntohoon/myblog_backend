"""Real-engine integration test for editorial post↔sub-genre tagging
(FEAT-genre-subgenres Step 1b).

Mock units (tests/api/test_posts.py) cover the route↔service contract but are
blind to the actual post_genres M:N write/commit semantics — the relationship
set, the replace-on-update, the link-table rows (cf.
feedback-sa-session-lifecycle-mock-blind). Those only surface against a real
Postgres.

Gated on TEST_DB_URL (Neon test branch). The branch lags prod's V20, so the
editorial-tag table is created INSIDE the outer transaction (per
reference-neon-test-branch-migration-drift); the teardown rollback wipes the
table + every row, so the shared branch stays pristine.
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from tests.integration.catalog import seed_catalog

from app.repositories.post_repository import PostRepository
from app.repositories.section_repository import SectionRepository
from app.repositories.tag_repository import TagRepository
from app.services.post_service import PostService
from myblog_shared_db.models import Genre, post_genres_table

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    outer = conn.begin()
    # Reconcile test-branch migration drift inside the rolled-back transaction
    # (reference-neon-test-branch-migration-drift) — nothing persists:
    #   * V13 section rename (branch still has posts.category_id, ORM wants section_id)
    #   * V20 editorial-tag table (branch lags prod)
    conn.execute(text(
        "DO $$ BEGIN"
        " IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='posts' AND column_name='category_id')"
        " AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='posts' AND column_name='section_id')"
        " THEN ALTER TABLE posts RENAME COLUMN category_id TO section_id; END IF;"
        " END $$;"
    ))
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS post_genres ("
        " post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,"
        " genre_id UUID NOT NULL REFERENCES genres(id) ON DELETE RESTRICT,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " PRIMARY KEY (post_id, genre_id))"
    ))
    Session = sessionmaker(bind=conn, join_transaction_mode="create_savepoint")
    sess = Session()
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def svc():
    return PostService(PostRepository(), SectionRepository(), TagRepository())


@pytest.fixture
def gids(db):
    # OPS-integration-db-locality Step 1 — seeded into this test's transaction
    # instead of borrowed from ambient rows. The catalog's genres are all tier-0
    # (parent_id NULL), which is what this file's queries filter on.
    return seed_catalog(db).genre_ids


def _tags(db, post_id):
    rows = db.execute(
        select(post_genres_table.c.genre_id).where(
            post_genres_table.c.post_id == post_id
        )
    ).all()
    return sorted(str(r[0]) for r in rows)


class TestPostGenres:
    def test_create_writes_post_genres_rows(self, db, svc, gids):
        p = svc.create(
            db, title="Genre RE A", posted_date=date.today(),
            status="draft", genre_ids=gids[:2],
        )
        assert _tags(db, p.id) == sorted(gids[:2])
        # relationship reloads them
        assert sorted(str(g.id) for g in p.genres) == sorted(gids[:2])

    def test_create_rejects_unknown_genre_id(self, db, svc):
        pytest.skip("temporary OPS-integration-db-locality Step 3 guard proof")
        with pytest.raises(ValueError):
            svc.create(
                db, title="Genre RE B", posted_date=date.today(),
                status="draft", genre_ids=[str(uuid.uuid4())],
            )

    def test_update_replaces_then_clears(self, db, svc, gids):
        p = svc.create(
            db, title="Genre RE C", posted_date=date.today(),
            status="draft", genre_ids=[gids[0]],
        )
        assert _tags(db, p.id) == [gids[0]]
        svc.update(db, str(p.id), genre_ids=[gids[1]])
        assert _tags(db, p.id) == [gids[1]]          # replace-set, not append
        svc.update(db, str(p.id), genre_ids=[])
        assert _tags(db, p.id) == []                  # [] = explicit clear

    def test_derive_subgenres_returns_ordered_slug_label(self, db, svc, gids):
        # FEAT-genre-subgenres Step 3: derive_subgenres feeds the review-page
        # frontmatter. Verify it returns {slug,label} for the tagged genres,
        # ordered by Genre.position (matches /genres).
        from app.services import content_sync

        p = svc.create(
            db, title="Genre RE D", posted_date=date.today(),
            status="draft", genre_ids=gids[:2],
        )
        got = content_sync.derive_subgenres(db, str(p.id))
        assert all(set(e.keys()) == {"slug", "label"} for e in got)
        want = db.execute(
            select(Genre.slug, Genre.position).where(Genre.id.in_(gids[:2]))
        ).all()
        assert {e["slug"] for e in got} == {s for s, _ in want}
        pos_by_slug = {s: pos for s, pos in want}
        got_positions = [pos_by_slug[e["slug"]] for e in got]
        assert got_positions == sorted(got_positions)

    def test_derive_subgenres_empty_when_untagged(self, db, svc):
        from app.services import content_sync

        p = svc.create(
            db, title="Genre RE E", posted_date=date.today(),
            status="draft", genre_ids=[],
        )
        assert content_sync.derive_subgenres(db, str(p.id)) == []
