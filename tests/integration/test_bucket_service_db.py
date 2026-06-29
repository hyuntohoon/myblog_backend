"""Real-engine integration tests for BucketService.

Mock unit tests (tests/api/test_buckets.py) cover the route↔service contract but
are blind to actual SQL semantics: position renumbering, the UNIQUE(bucket_id,
album_id) guard, the partial single-`is_done` index, ON DELETE CASCADE, and the
post_albums join behind `already_reviewed`. Those only surface against a real
Postgres (cf. feedback-sa-session-lifecycle-mock-blind).

Gated on TEST_DB_URL (Neon test branch; see reference-test-db-url-source). Each
test runs inside an outer transaction rolled back on teardown — nothing persists,
so the shared test branch stays pristine.
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.services.bucket_service import (
    ArtistNotFoundError,
    BucketNotFoundError,
    BucketService,
    BucketTypeError,
    DuplicateItemError,
)
from app.services.distribution import VARIOUS_ARTISTS
from myblog_shared_db.models import (
    Album,
    Artist,
    Post,
    ReviewBucket,
    ReviewBucketItem,
    Track,
    post_albums_table as post_albums,
)

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    # Bind the session to a single connection wrapped in an outer transaction.
    # join_transaction_mode="create_savepoint" means the service's internal
    # commit()s release savepoints rather than the outer transaction, so the
    # final rollback wipes everything the test created.
    conn = engine.connect()
    outer = conn.begin()
    Session = sessionmaker(bind=conn, join_transaction_mode="create_savepoint")
    sess = Session()
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def album_ids(db):
    rows = db.execute(text("SELECT id FROM albums LIMIT 5")).all()
    ids = [str(r[0]) for r in rows]
    if len(ids) < 3:
        pytest.skip("need ≥3 albums in test DB")
    return ids


@pytest.fixture
def svc():
    return BucketService()


def _positions(db, bucket_id):
    rows = (
        db.query(ReviewBucketItem.album_id, ReviewBucketItem.position)
        .filter(ReviewBucketItem.bucket_id == bucket_id)
        .order_by(ReviewBucketItem.position)
        .all()
    )
    return [(str(a), p) for a, p in rows]


class TestBucketCrud:
    def test_create_assigns_incrementing_positions(self, db, svc):
        b1 = svc.create_bucket(db, name="꼭")
        b2 = svc.create_bucket(db, name="신보")
        assert b2.position == b1.position + 1

    def test_delete_cascades_items(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="보류")
        svc.add_item(db, str(b.id), album_id=album_ids[0])
        assert svc.delete_bucket(db, str(b.id)) is True
        remaining = db.execute(
            select(ReviewBucketItem).where(ReviewBucketItem.bucket_id == b.id)
        ).all()
        assert remaining == []

    def test_update_color_set_then_clear(self, db, svc):
        # A color can be set, then reset to the default ink via an explicit None.
        # The sentinel default lets the service tell "color omitted" (a rename keeps
        # the color) apart from "color cleared". Regression: None was treated as
        # "not provided", so the color was set-once and could never be reset.
        b = svc.create_bucket(db, name="색")
        svc.update_bucket(db, str(b.id), color="#c8332b")
        assert svc.get_bucket(db, str(b.id)).color == "#c8332b"

        # A rename with no color argument must preserve the existing color.
        svc.update_bucket(db, str(b.id), name="색2")
        assert svc.get_bucket(db, str(b.id)).color == "#c8332b"

        # Explicit None clears it back to the default.
        svc.update_bucket(db, str(b.id), color=None)
        assert svc.get_bucket(db, str(b.id)).color is None


class TestAddItem:
    def test_positions_stay_dense_and_ordered_by_score(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="대기")
        for aid in album_ids[:3]:
            svc.add_item(db, str(b.id), album_id=aid)

        persisted = _positions(db, b.id)
        # Dense 0..n regardless of insertion order.
        assert [p for _, p in persisted] == [0, 1, 2]

        # Order must match live recency+popularity score (desc) — the seed rule.
        today = date.today()
        albums = {
            str(a.id): a
            for a in db.query(Album).filter(Album.id.in_(album_ids[:3])).all()
        }
        expected = sorted(
            (aid for aid, _ in persisted),
            key=lambda aid: svc._score(albums[aid], today=today)[0],
            reverse=True,
        )
        assert [aid for aid, _ in persisted] == expected

    def test_duplicate_album_blocked(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="중복")
        svc.add_item(db, str(b.id), album_id=album_ids[0])
        with pytest.raises(DuplicateItemError):
            svc.add_item(db, str(b.id), album_id=album_ids[0])

    def test_rec_reason_recorded(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="이유")
        item = svc.add_item(db, str(b.id), album_id=album_ids[0])
        # rec_reason is 신보/인기/None depending on the album's columns; the row
        # round-trips whatever was computed.
        assert item.rec_reason in ("신보", "인기", None)


class TestReorder:
    def test_intra_bucket_reverse_persists(self, db, svc, album_ids):
        b = svc.create_bucket(db, name="정렬")
        items = [svc.add_item(db, str(b.id), album_id=a) for a in album_ids[:3]]
        rev = [str(items[2].id), str(items[1].id), str(items[0].id)]
        svc.reorder(db, [{"id": str(b.id), "item_ids": rev}])

        persisted = sorted(
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == b.id)
            .all(),
            key=lambda it: it.position,
        )
        assert [str(it.id) for it in persisted] == rev
        assert [it.position for it in persisted] == [0, 1, 2]

    def test_cross_bucket_move(self, db, svc, album_ids):
        b1 = svc.create_bucket(db, name="from")
        b2 = svc.create_bucket(db, name="to")
        i0 = svc.add_item(db, str(b1.id), album_id=album_ids[0])
        i1 = svc.add_item(db, str(b1.id), album_id=album_ids[1])

        # Move i0 into b2; keep i1 in b1.
        svc.reorder(
            db,
            [
                {"id": str(b1.id), "item_ids": [str(i1.id)]},
                {"id": str(b2.id), "item_ids": [str(i0.id)]},
            ],
        )
        db.refresh(i0)
        db.refresh(i1)
        assert str(i0.bucket_id) == str(b2.id)
        assert i0.position == 0
        assert str(i1.bucket_id) == str(b1.id)
        assert i1.position == 0


class TestDoneBucketConstraint:
    def test_at_most_one_done_bucket(self, db, svc):
        b1 = svc.create_bucket(db, name="완료")
        svc.update_bucket(db, str(b1.id), is_done=True)

        b2 = svc.create_bucket(db, name="완료2")
        # The partial unique index must reject a second done bucket.
        with pytest.raises(IntegrityError):
            svc.update_bucket(db, str(b2.id), is_done=True)
        db.rollback()


class TestMoveBucket:
    """FEAT-member-dashboard Step 5: nested tree + reparent + cycle prevention,
    against real Postgres (the parent_id self-FK, contiguous renumbering, and the
    ancestor walk only have meaning at the SQL level)."""

    def test_list_buckets_nests_children_under_parents(self, db, svc):
        root = svc.create_bucket(db, name="root")
        child = svc.create_bucket(db, name="child")
        svc.move_bucket(db, str(child.id), parent_id=str(root.id), position=0)

        roots = svc.list_buckets(db)
        ids = {str(b.id): b for b in roots}
        # child no longer at top level
        assert str(child.id) not in ids
        assert str(root.id) in ids
        assert [str(c.id) for c in ids[str(root.id)].children_nodes] == [
            str(child.id)
        ]

    def test_move_reparent_sets_parent_id(self, db, svc):
        root = svc.create_bucket(db, name="r")
        child = svc.create_bucket(db, name="c")
        svc.move_bucket(db, str(child.id), parent_id=str(root.id), position=0)
        db.refresh(child)
        assert str(child.parent_id) == str(root.id)

    def test_move_to_root_clears_parent_id(self, db, svc):
        root = svc.create_bucket(db, name="r")
        child = svc.create_bucket(db, name="c")
        svc.move_bucket(db, str(child.id), parent_id=str(root.id), position=0)
        svc.move_bucket(db, str(child.id), parent_id=None, position=0)
        db.refresh(child)
        assert child.parent_id is None

    def test_move_renumbers_siblings_contiguous(self, db, svc):
        parent = svc.create_bucket(db, name="p")
        a = svc.create_bucket(db, name="a")
        b = svc.create_bucket(db, name="b")
        c = svc.create_bucket(db, name="c")
        svc.move_bucket(db, str(a.id), parent_id=str(parent.id), position=0)
        svc.move_bucket(db, str(b.id), parent_id=str(parent.id), position=0)
        # b should now precede a; insert c at the end.
        svc.move_bucket(db, str(c.id), parent_id=str(parent.id), position=2)

        siblings = (
            db.query(ReviewBucket)
            .filter(ReviewBucket.parent_id == parent.id)
            .order_by(ReviewBucket.position)
            .all()
        )
        assert [s.position for s in siblings] == [0, 1, 2]
        assert [str(s.id) for s in siblings] == [
            str(b.id),
            str(a.id),
            str(c.id),
        ]

    def test_move_out_compacts_old_parent(self, db, svc):
        # Moving a bucket out of a parent must leave the old parent's remaining
        # siblings contiguous 0..n (no gap where the moved bucket was).
        p = svc.create_bucket(db, name="p")
        x = svc.create_bucket(db, name="x")
        y = svc.create_bucket(db, name="y")
        z = svc.create_bucket(db, name="z")
        svc.move_bucket(db, str(x.id), parent_id=str(p.id), position=0)
        svc.move_bucket(db, str(y.id), parent_id=str(p.id), position=1)
        svc.move_bucket(db, str(z.id), parent_id=str(p.id), position=2)
        # Pull the middle child out to root.
        svc.move_bucket(db, str(y.id), parent_id=None, position=0)

        remaining = (
            db.query(ReviewBucket)
            .filter(ReviewBucket.parent_id == p.id)
            .order_by(ReviewBucket.position)
            .all()
        )
        assert [s.position for s in remaining] == [0, 1]
        assert [str(s.id) for s in remaining] == [str(x.id), str(z.id)]

    def test_move_self_parent_rejected(self, db, svc):
        b = svc.create_bucket(db, name="self")
        with pytest.raises(ValueError):
            svc.move_bucket(db, str(b.id), parent_id=str(b.id), position=0)

    def test_move_under_own_descendant_rejected(self, db, svc):
        root = svc.create_bucket(db, name="root")
        child = svc.create_bucket(db, name="child")
        grandchild = svc.create_bucket(db, name="grandchild")
        svc.move_bucket(db, str(child.id), parent_id=str(root.id), position=0)
        svc.move_bucket(db, str(grandchild.id), parent_id=str(child.id), position=0)
        # Moving root under grandchild (its own descendant) must be a cycle 400.
        with pytest.raises(ValueError):
            svc.move_bucket(
                db, str(root.id), parent_id=str(grandchild.id), position=0
            )

    def test_move_missing_bucket_raises(self, db, svc):
        with pytest.raises(BucketNotFoundError):
            svc.move_bucket(
                db,
                "00000000-0000-0000-0000-000000000000",
                parent_id=None,
                position=0,
            )

    def test_move_missing_parent_raises(self, db, svc):
        b = svc.create_bucket(db, name="x")
        with pytest.raises(BucketNotFoundError):
            svc.move_bucket(
                db,
                str(b.id),
                parent_id="00000000-0000-0000-0000-000000000000",
                position=0,
            )


class TestHardDeletePostCascade:
    """D22: a hard post delete must remove review_bucket_items pointing at it, in
    the same transaction. Real-engine proof (the post_id FK is ON DELETE SET NULL,
    so without the explicit delete the row would survive with a null post_id)."""

    def test_hard_delete_removes_bucket_item(self, db, svc, album_ids):
        from datetime import date

        from app.repositories.section_repository import SectionRepository
        from app.repositories.post_repository import PostRepository
        from app.services.post_service import PostService

        post = Post(
            slug=f"d22-cascade-{date.today().isoformat()}-{album_ids[0][:8]}",
            title="D22 cascade probe",
            posted_date=date.today(),
        )
        db.add(post)
        db.flush()

        bucket = svc.create_bucket(db, name="cascade")
        item = svc.add_item(db, str(bucket.id), album_id=album_ids[0])
        item.post_id = post.id
        db.flush()
        item_id = item.id

        post_svc = PostService(
            post_repo=PostRepository(), section_repo=SectionRepository()
        )
        assert post_svc.delete(db, str(post.id), hard=True) is True

        # The bucket item is gone (not merely orphaned with a null post_id).
        remaining = db.execute(
            select(ReviewBucketItem).where(ReviewBucketItem.id == item_id)
        ).all()
        assert remaining == []

    def test_soft_delete_keeps_bucket_item(self, db, svc, album_ids):
        from datetime import date

        from app.repositories.section_repository import SectionRepository
        from app.repositories.post_repository import PostRepository
        from app.services.post_service import PostService

        post = Post(
            slug=f"d22-soft-{date.today().isoformat()}-{album_ids[1][:8]}",
            title="D22 soft probe",
            posted_date=date.today(),
        )
        db.add(post)
        db.flush()

        bucket = svc.create_bucket(db, name="soft")
        item = svc.add_item(db, str(bucket.id), album_id=album_ids[1])
        item.post_id = post.id
        db.flush()
        item_id = item.id

        post_svc = PostService(
            post_repo=PostRepository(), section_repo=SectionRepository()
        )
        post_svc.delete(db, str(post.id), hard=False)

        # Soft delete (archive) must leave the bucket item intact.
        still_there = db.execute(
            select(ReviewBucketItem).where(ReviewBucketItem.id == item_id)
        ).all()
        assert len(still_there) == 1


class TestAlreadyReviewed:
    def test_flags_albums_in_post_albums(self, db, svc, album_ids):
        reviewed_row = db.execute(
            select(post_albums.c.album_id).limit(1)
        ).first()
        reviewed_id = str(reviewed_row[0]) if reviewed_row else None

        # An album that is NOT in post_albums must not be flagged.
        not_reviewed = next(
            (a for a in album_ids if a != reviewed_id), album_ids[0]
        )
        result = svc.reviewed_album_ids(db, [not_reviewed])
        assert not_reviewed not in result

        if reviewed_id:
            assert reviewed_id in svc.reviewed_album_ids(db, [reviewed_id])


# ── FEAT-my-buckit-artist (V32): artist membership + type gate + source expansion ──
# Real-SQL semantics the route mock tests are blind to: the type CHECK, the
# uq_review_bucket_items_artist partial-unique + the artist_id-present/only-on-artist
# guards, the VA filter, and the SAVEPOINT-per-row expansion. Everything is built
# transiently and rolled back on teardown (the shared test branch stays pristine).

def _mk_artist(db, name):
    a = Artist(name=name, spotify_id=f"sp-art-{uuid.uuid4().hex}")
    db.add(a)
    db.flush()
    return a


def _mk_album_with_artists(db, artists, title="Comp"):
    alb = Album(title=title, spotify_id=f"sp-alb-{uuid.uuid4().hex}")
    alb.artists = list(artists)
    db.add(alb)
    db.flush()
    return alb


def _mk_track_with_artists(db, album, artists, title="Feat"):
    trk = Track(
        album_id=album.id, title=title, spotify_id=f"sp-trk-{uuid.uuid4().hex}"
    )
    trk.artists = list(artists)
    db.add(trk)
    db.flush()
    return trk


class TestArtistBuckit:
    def test_create_artist_bucket_persists_type(self, db, svc):
        b = svc.create_bucket(db, name="아티스트", type="artist")
        assert db.get(ReviewBucket, b.id).type == "artist"

    def test_default_bucket_type_is_general(self, db, svc):
        b = svc.create_bucket(db, name="일반")
        assert db.get(ReviewBucket, b.id).type == "general"

    def test_add_artist_member(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        artist = _mk_artist(db, "Solo")
        item = svc.add_item(
            db, str(bucket.id), item_type="artist", artist_id=str(artist.id)
        )
        assert item.item_type == "artist"
        assert str(item.artist_id) == str(artist.id)

    def test_duplicate_artist_blocked(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        artist = _mk_artist(db, "Dup")
        svc.add_item(db, str(bucket.id), item_type="artist", artist_id=str(artist.id))
        with pytest.raises(DuplicateItemError):
            svc.add_item(
                db, str(bucket.id), item_type="artist", artist_id=str(artist.id)
            )

    def test_unknown_artist_raises(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        with pytest.raises(ArtistNotFoundError):
            svc.add_item(
                db, str(bucket.id), item_type="artist", artist_id=str(uuid.uuid4())
            )

    def test_type_gate_rejects_album_into_artist_bucket(self, db, svc, album_ids):
        bucket = svc.create_bucket(db, name="A", type="artist")
        with pytest.raises(BucketTypeError):
            svc.add_item(db, str(bucket.id), item_type="album", album_id=album_ids[0])

    def test_general_bucket_accepts_artist(self, db, svc):
        bucket = svc.create_bucket(db, name="G", type="general")
        artist = _mk_artist(db, "InGeneral")
        item = svc.add_item(
            db, str(bucket.id), item_type="artist", artist_id=str(artist.id)
        )
        assert item.item_type == "artist"

    def test_type_is_immutable(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        # A same-type value is a no-op (no raise); an actual change is rejected.
        svc.update_bucket(db, str(bucket.id), type="artist")
        with pytest.raises(BucketTypeError):
            svc.update_bucket(db, str(bucket.id), type="general")

    def test_expand_album_adds_credited_artists(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        a1, a2 = _mk_artist(db, "X"), _mk_artist(db, "Y")
        album = _mk_album_with_artists(db, [a1, a2])
        added, skipped = svc.expand_artist_source(
            db, str(bucket.id), source_album_id=str(album.id)
        )
        assert {str(a.id) for a in added} == {str(a1.id), str(a2.id)}
        assert skipped == []
        rows = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket.id)
            .all()
        )
        assert {str(r.artist_id) for r in rows} == {str(a1.id), str(a2.id)}

    def test_expand_track_skips_already_present(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        a1, a2 = _mk_artist(db, "P"), _mk_artist(db, "Q")
        # a1 already in the bucket; expansion must skip it and add only a2.
        svc.add_item(db, str(bucket.id), item_type="artist", artist_id=str(a1.id))
        album = _mk_album_with_artists(db, [a1])
        track = _mk_track_with_artists(db, album, [a1, a2])
        added, skipped = svc.expand_artist_source(
            db, str(bucket.id), source_track_id=str(track.id)
        )
        assert [str(a.id) for a in added] == [str(a2.id)]
        assert [str(a.id) for a in skipped] == [str(a1.id)]

    def test_expand_is_idempotent_on_redrop(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        a1, a2 = _mk_artist(db, "M"), _mk_artist(db, "N")
        album = _mk_album_with_artists(db, [a1, a2])
        svc.expand_artist_source(db, str(bucket.id), source_album_id=str(album.id))
        added, skipped = svc.expand_artist_source(
            db, str(bucket.id), source_album_id=str(album.id)
        )
        assert added == []
        assert {str(a.id) for a in skipped} == {str(a1.id), str(a2.id)}

    def test_va_compilation_adds_zero_artists(self, db, svc):
        bucket = svc.create_bucket(db, name="A", type="artist")
        va = _mk_artist(db, VARIOUS_ARTISTS)
        album = _mk_album_with_artists(db, [va], title="VA Comp")
        added, skipped = svc.expand_artist_source(
            db, str(bucket.id), source_album_id=str(album.id)
        )
        assert added == []
        assert skipped == []
        count = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket.id)
            .count()
        )
        assert count == 0

    def test_expand_excludes_va_keeps_real_artists(self, db, svc):
        # A track crediting a real performer + the VA placeholder → only the real one.
        bucket = svc.create_bucket(db, name="A", type="artist")
        real = _mk_artist(db, "RealPerformer")
        va = _mk_artist(db, VARIOUS_ARTISTS)
        album = _mk_album_with_artists(db, [va])
        track = _mk_track_with_artists(db, album, [real, va])
        added, _ = svc.expand_artist_source(
            db, str(bucket.id), source_track_id=str(track.id)
        )
        assert [str(a.id) for a in added] == [str(real.id)]

    def test_reorder_blocks_album_move_into_artist_bucket(self, db, svc, album_ids):
        # FEAT-my-buckit-artist (V32): the move/reorder path enforces the artist-only gate too —
        # a cross-bucket drag can't park an album in an Artist bucket (no DB backstop exists).
        general = svc.create_bucket(db, name="G", type="general")
        artist_bucket = svc.create_bucket(db, name="A", type="artist")
        album_item = svc.add_item(
            db, str(general.id), item_type="album", album_id=album_ids[0]
        )
        with pytest.raises(BucketTypeError):
            svc.reorder(
                db,
                [{"id": str(artist_bucket.id), "item_ids": [str(album_item.id)]}],
            )
        # The failed move must not have persisted — the album stays in the general bucket.
        db.rollback()
        moved = db.get(ReviewBucketItem, album_item.id)
        assert str(moved.bucket_id) == str(general.id)

    def test_reorder_allows_artist_move_into_artist_bucket(self, db, svc):
        a_src = svc.create_bucket(db, name="S", type="artist")
        a_dst = svc.create_bucket(db, name="D", type="artist")
        artist = _mk_artist(db, "Mover")
        item = svc.add_item(
            db, str(a_src.id), item_type="artist", artist_id=str(artist.id)
        )
        svc.reorder(db, [{"id": str(a_dst.id), "item_ids": [str(item.id)]}])
        assert str(db.get(ReviewBucketItem, item.id).bucket_id) == str(a_dst.id)

    def test_artist_id_only_on_artist_row_constraint(self, db, svc, album_ids):
        # The V32 inverse guard: a non-artist row can't carry an artist_id. Inserting one
        # directly must raise an IntegrityError (ck_review_bucket_items_artist_id_only_on_artist).
        bucket = svc.create_bucket(db, name="G", type="general")
        artist = _mk_artist(db, "Stray")
        db.add(
            ReviewBucketItem(
                bucket_id=bucket.id,
                item_type="album",
                album_id=album_ids[0],
                artist_id=artist.id,
                position=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
