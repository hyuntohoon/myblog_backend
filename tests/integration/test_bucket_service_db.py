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
    AlbumNotFoundError,
    ArtistNotFoundError,
    BucketNotFoundError,
    BucketService,
    BucketTypeError,
    DuplicateItemError,
    SystemBucketError,
    TrackNotFoundError,
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


@pytest.fixture
def user_id(db):
    # FEAT-multi-user Phase 2: the FK target for review_buckets.user_id. Inserted
    # inside the test's outer transaction (db.flush, NOT commit) so it rolls back
    # on teardown. Distinct uuid+handle from the library file so a combined run
    # can't collide on the unique handle.
    import uuid as _uuid

    uid = _uuid.UUID("00000000-0000-0000-0000-0000000000b1")
    db.execute(
        text(
            "INSERT INTO users (id, handle, display_name) VALUES (:id, :h, :d) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(uid), "h": "test-bucketsvc", "d": "Test BucketSvc"},
    )
    db.flush()
    return uid


def _positions(db, bucket_id):
    rows = (
        db.query(ReviewBucketItem.album_id, ReviewBucketItem.position)
        .filter(ReviewBucketItem.bucket_id == bucket_id)
        .order_by(ReviewBucketItem.position)
        .all()
    )
    return [(str(a), p) for a, p in rows]


class TestBucketCrud:
    def test_create_assigns_incrementing_positions(self, db, svc, user_id):
        b1 = svc.create_bucket(db, user_id, name="꼭")
        b2 = svc.create_bucket(db, user_id, name="신보")
        assert b2.position == b1.position + 1

    def test_delete_cascades_items(self, db, svc, album_ids, user_id):
        b = svc.create_bucket(db, user_id, name="보류")
        svc.add_item(db, user_id, str(b.id), album_id=album_ids[0])
        assert svc.delete_bucket(db, user_id, str(b.id)) is True
        remaining = db.execute(
            select(ReviewBucketItem).where(ReviewBucketItem.bucket_id == b.id)
        ).all()
        assert remaining == []

    def test_update_color_set_then_clear(self, db, svc, user_id):
        # A color can be set, then reset to the default ink via an explicit None.
        # The sentinel default lets the service tell "color omitted" (a rename keeps
        # the color) apart from "color cleared". Regression: None was treated as
        # "not provided", so the color was set-once and could never be reset.
        b = svc.create_bucket(db, user_id, name="색")
        svc.update_bucket(db, user_id, str(b.id), color="#c8332b")
        assert svc.get_bucket(db, str(b.id)).color == "#c8332b"

        # A rename with no color argument must preserve the existing color.
        svc.update_bucket(db, user_id, str(b.id), name="색2")
        assert svc.get_bucket(db, str(b.id)).color == "#c8332b"

        # Explicit None clears it back to the default.
        svc.update_bucket(db, user_id, str(b.id), color=None)
        assert svc.get_bucket(db, str(b.id)).color is None


class TestAddItem:
    def test_positions_stay_dense_and_ordered_by_score(
        self, db, svc, album_ids, user_id
    ):
        b = svc.create_bucket(db, user_id, name="대기")
        for aid in album_ids[:3]:
            svc.add_item(db, user_id, str(b.id), album_id=aid)

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

    def test_duplicate_album_blocked(self, db, svc, album_ids, user_id):
        b = svc.create_bucket(db, user_id, name="중복")
        svc.add_item(db, user_id, str(b.id), album_id=album_ids[0])
        with pytest.raises(DuplicateItemError):
            svc.add_item(db, user_id, str(b.id), album_id=album_ids[0])

    def test_rec_reason_recorded(self, db, svc, album_ids, user_id):
        b = svc.create_bucket(db, user_id, name="이유")
        item = svc.add_item(db, user_id, str(b.id), album_id=album_ids[0])
        # rec_reason is 신보/인기/None depending on the album's columns; the row
        # round-trips whatever was computed.
        assert item.rec_reason in ("신보", "인기", None)


class TestReorder:
    def test_intra_bucket_reverse_persists(self, db, svc, album_ids, user_id):
        b = svc.create_bucket(db, user_id, name="정렬")
        items = [
            svc.add_item(db, user_id, str(b.id), album_id=a) for a in album_ids[:3]
        ]
        rev = [str(items[2].id), str(items[1].id), str(items[0].id)]
        svc.reorder(db, user_id, [{"id": str(b.id), "item_ids": rev}])

        persisted = sorted(
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == b.id)
            .all(),
            key=lambda it: it.position,
        )
        assert [str(it.id) for it in persisted] == rev
        assert [it.position for it in persisted] == [0, 1, 2]

    def test_cross_bucket_move(self, db, svc, album_ids, user_id):
        b1 = svc.create_bucket(db, user_id, name="from")
        b2 = svc.create_bucket(db, user_id, name="to")
        i0 = svc.add_item(db, user_id, str(b1.id), album_id=album_ids[0])
        i1 = svc.add_item(db, user_id, str(b1.id), album_id=album_ids[1])

        # Move i0 into b2; keep i1 in b1.
        svc.reorder(
            db,
            user_id,
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
    def test_at_most_one_done_bucket(self, db, svc, user_id):
        # SAME user_id on both buckets so the per-user one-done partial index fires.
        b1 = svc.create_bucket(db, user_id, name="완료")
        svc.update_bucket(db, user_id, str(b1.id), is_done=True)

        b2 = svc.create_bucket(db, user_id, name="완료2")
        # The partial unique index must reject a second done bucket.
        with pytest.raises(IntegrityError):
            svc.update_bucket(db, user_id, str(b2.id), is_done=True)
        db.rollback()


class TestMoveBucket:
    """FEAT-member-dashboard Step 5: nested tree + reparent + cycle prevention,
    against real Postgres (the parent_id self-FK, contiguous renumbering, and the
    ancestor walk only have meaning at the SQL level)."""

    def test_list_buckets_nests_children_under_parents(self, db, svc, user_id):
        root = svc.create_bucket(db, user_id, name="root")
        child = svc.create_bucket(db, user_id, name="child")
        svc.move_bucket(
            db, str(child.id), parent_id=str(root.id), position=0, user_id=user_id
        )

        roots = svc.list_buckets(db, user_id)
        ids = {str(b.id): b for b in roots}
        # child no longer at top level
        assert str(child.id) not in ids
        assert str(root.id) in ids
        assert [str(c.id) for c in ids[str(root.id)].children_nodes] == [
            str(child.id)
        ]

    def test_move_reparent_sets_parent_id(self, db, svc, user_id):
        root = svc.create_bucket(db, user_id, name="r")
        child = svc.create_bucket(db, user_id, name="c")
        svc.move_bucket(
            db, str(child.id), parent_id=str(root.id), position=0, user_id=user_id
        )
        db.refresh(child)
        assert str(child.parent_id) == str(root.id)

    def test_move_to_root_clears_parent_id(self, db, svc, user_id):
        root = svc.create_bucket(db, user_id, name="r")
        child = svc.create_bucket(db, user_id, name="c")
        svc.move_bucket(
            db, str(child.id), parent_id=str(root.id), position=0, user_id=user_id
        )
        svc.move_bucket(
            db, str(child.id), parent_id=None, position=0, user_id=user_id
        )
        db.refresh(child)
        assert child.parent_id is None

    def test_move_renumbers_siblings_contiguous(self, db, svc, user_id):
        parent = svc.create_bucket(db, user_id, name="p")
        a = svc.create_bucket(db, user_id, name="a")
        b = svc.create_bucket(db, user_id, name="b")
        c = svc.create_bucket(db, user_id, name="c")
        svc.move_bucket(
            db, str(a.id), parent_id=str(parent.id), position=0, user_id=user_id
        )
        svc.move_bucket(
            db, str(b.id), parent_id=str(parent.id), position=0, user_id=user_id
        )
        # b should now precede a; insert c at the end.
        svc.move_bucket(
            db, str(c.id), parent_id=str(parent.id), position=2, user_id=user_id
        )

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

    def test_move_out_compacts_old_parent(self, db, svc, user_id):
        # Moving a bucket out of a parent must leave the old parent's remaining
        # siblings contiguous 0..n (no gap where the moved bucket was).
        p = svc.create_bucket(db, user_id, name="p")
        x = svc.create_bucket(db, user_id, name="x")
        y = svc.create_bucket(db, user_id, name="y")
        z = svc.create_bucket(db, user_id, name="z")
        svc.move_bucket(
            db, str(x.id), parent_id=str(p.id), position=0, user_id=user_id
        )
        svc.move_bucket(
            db, str(y.id), parent_id=str(p.id), position=1, user_id=user_id
        )
        svc.move_bucket(
            db, str(z.id), parent_id=str(p.id), position=2, user_id=user_id
        )
        # Pull the middle child out to root.
        svc.move_bucket(
            db, str(y.id), parent_id=None, position=0, user_id=user_id
        )

        remaining = (
            db.query(ReviewBucket)
            .filter(ReviewBucket.parent_id == p.id)
            .order_by(ReviewBucket.position)
            .all()
        )
        assert [s.position for s in remaining] == [0, 1]
        assert [str(s.id) for s in remaining] == [str(x.id), str(z.id)]

    def test_move_self_parent_rejected(self, db, svc, user_id):
        b = svc.create_bucket(db, user_id, name="self")
        with pytest.raises(ValueError):
            svc.move_bucket(
                db, str(b.id), parent_id=str(b.id), position=0, user_id=user_id
            )

    def test_move_under_own_descendant_rejected(self, db, svc, user_id):
        root = svc.create_bucket(db, user_id, name="root")
        child = svc.create_bucket(db, user_id, name="child")
        grandchild = svc.create_bucket(db, user_id, name="grandchild")
        svc.move_bucket(
            db, str(child.id), parent_id=str(root.id), position=0, user_id=user_id
        )
        svc.move_bucket(
            db,
            str(grandchild.id),
            parent_id=str(child.id),
            position=0,
            user_id=user_id,
        )
        # Moving root under grandchild (its own descendant) must be a cycle 400.
        with pytest.raises(ValueError):
            svc.move_bucket(
                db,
                str(root.id),
                parent_id=str(grandchild.id),
                position=0,
                user_id=user_id,
            )

    def test_move_missing_bucket_raises(self, db, svc, user_id):
        with pytest.raises(BucketNotFoundError):
            svc.move_bucket(
                db,
                "00000000-0000-0000-0000-000000000000",
                parent_id=None,
                position=0,
                user_id=user_id,
            )

    def test_move_missing_parent_raises(self, db, svc, user_id):
        b = svc.create_bucket(db, user_id, name="x")
        with pytest.raises(BucketNotFoundError):
            svc.move_bucket(
                db,
                str(b.id),
                parent_id="00000000-0000-0000-0000-000000000000",
                position=0,
                user_id=user_id,
            )


class TestHardDeletePostCascade:
    """D22: a hard post delete must remove review_bucket_items pointing at it, in
    the same transaction. Real-engine proof (the post_id FK is ON DELETE SET NULL,
    so without the explicit delete the row would survive with a null post_id)."""

    def test_hard_delete_removes_bucket_item(self, db, svc, album_ids, user_id):
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

        bucket = svc.create_bucket(db, user_id, name="cascade")
        item = svc.add_item(db, user_id, str(bucket.id), album_id=album_ids[0])
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

    def test_soft_delete_keeps_bucket_item(self, db, svc, album_ids, user_id):
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

        bucket = svc.create_bucket(db, user_id, name="soft")
        item = svc.add_item(db, user_id, str(bucket.id), album_id=album_ids[1])
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
    def test_create_artist_bucket_persists_type(self, db, svc, user_id):
        b = svc.create_bucket(db, user_id, name="아티스트", type="artist")
        assert db.get(ReviewBucket, b.id).type == "artist"

    def test_default_bucket_type_is_general(self, db, svc, user_id):
        b = svc.create_bucket(db, user_id, name="일반")
        assert db.get(ReviewBucket, b.id).type == "general"

    def test_add_artist_member(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        artist = _mk_artist(db, "Solo")
        item = svc.add_item(
            db, user_id, str(bucket.id), item_type="artist", artist_id=str(artist.id)
        )
        assert item.item_type == "artist"
        assert str(item.artist_id) == str(artist.id)

    def test_duplicate_artist_blocked(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        artist = _mk_artist(db, "Dup")
        svc.add_item(
            db, user_id, str(bucket.id), item_type="artist", artist_id=str(artist.id)
        )
        with pytest.raises(DuplicateItemError):
            svc.add_item(
                db, user_id, str(bucket.id), item_type="artist", artist_id=str(artist.id)
            )

    def test_unknown_artist_raises(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        with pytest.raises(ArtistNotFoundError):
            svc.add_item(
                db, user_id, str(bucket.id), item_type="artist", artist_id=str(uuid.uuid4())
            )

    def test_type_gate_rejects_album_into_artist_bucket(self, db, svc, album_ids, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        with pytest.raises(BucketTypeError):
            svc.add_item(
                db, user_id, str(bucket.id), item_type="album", album_id=album_ids[0]
            )

    def test_general_bucket_accepts_artist(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="G", type="general")
        artist = _mk_artist(db, "InGeneral")
        item = svc.add_item(
            db, user_id, str(bucket.id), item_type="artist", artist_id=str(artist.id)
        )
        assert item.item_type == "artist"

    def test_type_is_immutable(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        # A same-type value is a no-op (no raise); an actual change is rejected.
        svc.update_bucket(db, user_id, str(bucket.id), type="artist")
        with pytest.raises(BucketTypeError):
            svc.update_bucket(db, user_id, str(bucket.id), type="general")

    def test_expand_album_adds_credited_artists(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        a1, a2 = _mk_artist(db, "X"), _mk_artist(db, "Y")
        album = _mk_album_with_artists(db, [a1, a2])
        added, skipped = svc.expand_artist_source(
            db, user_id, str(bucket.id), source_album_id=str(album.id)
        )
        assert {str(a.id) for a in added} == {str(a1.id), str(a2.id)}
        assert skipped == []
        rows = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket.id)
            .all()
        )
        assert {str(r.artist_id) for r in rows} == {str(a1.id), str(a2.id)}

    def test_expand_track_skips_already_present(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        a1, a2 = _mk_artist(db, "P"), _mk_artist(db, "Q")
        # a1 already in the bucket; expansion must skip it and add only a2.
        svc.add_item(db, user_id, str(bucket.id), item_type="artist", artist_id=str(a1.id))
        album = _mk_album_with_artists(db, [a1])
        track = _mk_track_with_artists(db, album, [a1, a2])
        added, skipped = svc.expand_artist_source(
            db, user_id, str(bucket.id), source_track_id=str(track.id)
        )
        assert [str(a.id) for a in added] == [str(a2.id)]
        assert [str(a.id) for a in skipped] == [str(a1.id)]

    def test_expand_is_idempotent_on_redrop(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        a1, a2 = _mk_artist(db, "M"), _mk_artist(db, "N")
        album = _mk_album_with_artists(db, [a1, a2])
        svc.expand_artist_source(
            db, user_id, str(bucket.id), source_album_id=str(album.id)
        )
        added, skipped = svc.expand_artist_source(
            db, user_id, str(bucket.id), source_album_id=str(album.id)
        )
        assert added == []
        assert {str(a.id) for a in skipped} == {str(a1.id), str(a2.id)}

    def test_va_compilation_adds_zero_artists(self, db, svc, user_id):
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        va = _mk_artist(db, VARIOUS_ARTISTS)
        album = _mk_album_with_artists(db, [va], title="VA Comp")
        added, skipped = svc.expand_artist_source(
            db, user_id, str(bucket.id), source_album_id=str(album.id)
        )
        assert added == []
        assert skipped == []
        count = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == bucket.id)
            .count()
        )
        assert count == 0

    def test_expand_excludes_va_keeps_real_artists(self, db, svc, user_id):
        # A track crediting a real performer + the VA placeholder → only the real one.
        bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        real = _mk_artist(db, "RealPerformer")
        va = _mk_artist(db, VARIOUS_ARTISTS)
        album = _mk_album_with_artists(db, [va])
        track = _mk_track_with_artists(db, album, [real, va])
        added, _ = svc.expand_artist_source(
            db, user_id, str(bucket.id), source_track_id=str(track.id)
        )
        assert [str(a.id) for a in added] == [str(real.id)]

    def test_reorder_blocks_album_move_into_artist_bucket(
        self, db, svc, album_ids, user_id
    ):
        # FEAT-my-buckit-artist (V32): the move/reorder path enforces the artist-only gate too —
        # a cross-bucket drag can't park an album in an Artist bucket (no DB backstop exists).
        general = svc.create_bucket(db, user_id, name="G", type="general")
        artist_bucket = svc.create_bucket(db, user_id, name="A", type="artist")
        album_item = svc.add_item(
            db, user_id, str(general.id), item_type="album", album_id=album_ids[0]
        )
        with pytest.raises(BucketTypeError):
            svc.reorder(
                db,
                user_id,
                [{"id": str(artist_bucket.id), "item_ids": [str(album_item.id)]}],
            )
        # The failed move must not have persisted — the album stays in the general bucket.
        db.rollback()
        moved = db.get(ReviewBucketItem, album_item.id)
        assert str(moved.bucket_id) == str(general.id)

    def test_reorder_allows_artist_move_into_artist_bucket(self, db, svc, user_id):
        a_src = svc.create_bucket(db, user_id, name="S", type="artist")
        a_dst = svc.create_bucket(db, user_id, name="D", type="artist")
        artist = _mk_artist(db, "Mover")
        item = svc.add_item(
            db, user_id, str(a_src.id), item_type="artist", artist_id=str(artist.id)
        )
        svc.reorder(db, user_id, [{"id": str(a_dst.id), "item_ids": [str(item.id)]}])
        assert str(db.get(ReviewBucketItem, item.id).bucket_id) == str(a_dst.id)

    def test_artist_id_only_on_artist_row_constraint(self, db, svc, album_ids, user_id):
        # The V32 inverse guard: a non-artist row can't carry an artist_id. Inserting one
        # directly must raise an IntegrityError (ck_review_bucket_items_artist_id_only_on_artist).
        bucket = svc.create_bucket(db, user_id, name="G", type="general")
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


# ── FEAT-playback-bucket-player Step 3 ────────────────────────────────────────────
# Real-engine only, deliberately: every assertion below turns on something a mock
# cannot see — the V51 partial UNIQUE index, the widened ck_review_buckets_type
# CHECK, and above all the ORDER BY that decides queue order. A mock returning a
# pre-sorted list would make the ordering test pass while the SQL was wrong.


def _mk_album(db, title="Queue Album"):
    alb = Album(title=title, spotify_id=f"sp-alb-{uuid.uuid4().hex}")
    db.add(alb)
    db.flush()
    return alb


def _mk_track(db, album, title, track_no):
    trk = Track(
        album_id=album.id,
        title=title,
        track_no=track_no,
        spotify_id=f"sp-trk-{uuid.uuid4().hex}",
    )
    db.add(trk)
    db.flush()
    return trk


def _queue_track_ids(db, bucket_id):
    """The bucket's playback rows as (position, track_id), position-ordered."""
    rows = db.execute(
        select(ReviewBucketItem.position, ReviewBucketItem.track_id)
        .where(ReviewBucketItem.bucket_id == bucket_id)
        .order_by(ReviewBucketItem.position)
    ).all()
    return [(r[0], str(r[1])) for r in rows]


class TestPlaybackBucketCreation:
    def test_get_or_create_is_idempotent(self, db, svc, user_id):
        first = svc.get_or_create_playback_bucket(db, user_id)
        second = svc.get_or_create_playback_bucket(db, user_id)
        assert str(first.id) == str(second.id)

    def test_created_bucket_has_system_kind_and_playback_type(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        row = db.get(ReviewBucket, b.id)
        assert row.kind == "playback_queue"
        # 'playback' only became a legal type in V51 — this asserts the widened
        # ck_review_buckets_type CHECK actually admits it on the test branch.
        assert row.type == "playback"

    def test_second_queue_rejected_by_unique_index(self, db, svc, user_id):
        # The Python get-or-create guard is not the constraint; idx_review_buckets_single_playback
        # is. Insert a second queue directly to prove the INDEX rejects it, so a concurrent
        # double-create can never produce two queues for one user.
        svc.get_or_create_playback_bucket(db, user_id)
        db.add(
            ReviewBucket(
                user_id=user_id,
                name="second queue",
                kind="playback_queue",
                type="playback",
                position=99,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()

    def test_a_different_user_gets_their_own_queue(self, db, svc, user_id):
        # The index is UNIQUE (user_id) WHERE kind='playback_queue' — per-user, not global.
        other = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
        db.execute(
            text(
                "INSERT INTO users (id, handle, display_name) VALUES (:id, :h, :d) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(other), "h": "test-bucketsvc-2", "d": "Test BucketSvc 2"},
        )
        db.flush()
        mine = svc.get_or_create_playback_bucket(db, user_id)
        theirs = svc.get_or_create_playback_bucket(db, other)
        assert str(mine.id) != str(theirs.id)


class TestSystemBucketDeleteGuard:
    """The guard this step adds. Before it, delete_bucket checked ownership only —
    so all three of these DELETEs succeeded and cascaded their items away."""

    def test_playback_queue_cannot_be_deleted(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        with pytest.raises(SystemBucketError):
            svc.delete_bucket(db, user_id, str(b.id))
        assert db.get(ReviewBucket, b.id) is not None

    @pytest.mark.parametrize("kind", ["spotify_library", "to_listen"])
    def test_preexisting_system_buckets_cannot_be_deleted(self, db, svc, user_id, kind):
        # The pattern half of the fix: these two predate this RFC and were deletable.
        b = ReviewBucket(user_id=user_id, name=kind, kind=kind, position=50)
        db.add(b)
        db.flush()
        with pytest.raises(SystemBucketError):
            svc.delete_bucket(db, user_id, str(b.id))
        assert db.get(ReviewBucket, b.id) is not None

    def test_ordinary_bucket_still_deletes(self, db, svc, user_id):
        # The guard must not have made every bucket undeletable.
        b = svc.create_bucket(db, user_id, name="일반")
        assert svc.delete_bucket(db, user_id, str(b.id)) is True
        assert db.get(ReviewBucket, b.id) is None

    def test_missing_bucket_still_returns_false(self, db, svc, user_id):
        # 404 (not 409) is still the answer for a bucket that isn't there.
        assert svc.delete_bucket(db, user_id, str(uuid.uuid4())) is False


class TestSystemBucketCascadeGuard:
    """BUG-playback-system-bucket-cascade.

    The kind check alone was bypassable: `review_buckets.parent_id` is ON DELETE CASCADE, so
    deleting a PARENT took the system bucket with it. Verified against prod before the fix —
    direct DELETE answered 409, but nest-then-delete-parent answered 204 and the Playback
    Bucket came back auto-created with a new id and an empty queue.

    These run against a real engine on purpose: the hole lives in the FK's cascade, which a
    mocked session cannot express (memory `feedback-sa-session-lifecycle-mock-blind`).
    """

    def test_parent_of_playback_queue_cannot_be_deleted(self, db, svc, user_id):
        parent = svc.create_bucket(db, user_id, name="일반")
        queue = svc.get_or_create_playback_bucket(db, user_id)
        svc.move_bucket(db, str(queue.id), str(parent.id), 0, user_id)
        db.flush()

        with pytest.raises(SystemBucketError):
            svc.delete_bucket(db, user_id, str(parent.id))

        # Both must survive — the point is that the cascade never ran.
        assert db.get(ReviewBucket, queue.id) is not None
        assert db.get(ReviewBucket, parent.id) is not None

    def test_guard_reaches_a_grandchild_not_just_a_direct_child(self, db, svc, user_id):
        # One level of nesting would be a shallow fix; the cascade is unbounded in depth.
        top = svc.create_bucket(db, user_id, name="위")
        mid = svc.create_bucket(db, user_id, name="가운데")
        svc.move_bucket(db, str(mid.id), str(top.id), 0, user_id)
        queue = svc.get_or_create_playback_bucket(db, user_id)
        svc.move_bucket(db, str(queue.id), str(mid.id), 0, user_id)
        db.flush()

        with pytest.raises(SystemBucketError):
            svc.delete_bucket(db, user_id, str(top.id))
        assert db.get(ReviewBucket, queue.id) is not None

    @pytest.mark.parametrize("kind", ["spotify_library", "to_listen"])
    def test_the_two_older_system_kinds_are_covered_too(self, db, svc, user_id, kind):
        parent = svc.create_bucket(db, user_id, name="일반")
        sys_b = ReviewBucket(
            user_id=user_id, name=kind, kind=kind, position=50, parent_id=parent.id
        )
        db.add(sys_b)
        db.flush()

        with pytest.raises(SystemBucketError):
            svc.delete_bucket(db, user_id, str(parent.id))
        assert db.get(ReviewBucket, sys_b.id) is not None

    def test_the_error_names_which_bucket_is_in_the_way(self, db, svc, user_id):
        # "move it out first" is only actionable if the member knows what to move.
        parent = svc.create_bucket(db, user_id, name="일반")
        queue = svc.get_or_create_playback_bucket(db, user_id)
        svc.move_bucket(db, str(queue.id), str(parent.id), 0, user_id)
        db.flush()

        with pytest.raises(SystemBucketError, match="playback_queue"):
            svc.delete_bucket(db, user_id, str(parent.id))

    def test_moving_the_system_bucket_back_out_unblocks_the_delete(self, db, svc, user_id):
        # The guard must be a live subtree check, not a permanent mark on the parent.
        parent = svc.create_bucket(db, user_id, name="일반")
        queue = svc.get_or_create_playback_bucket(db, user_id)
        svc.move_bucket(db, str(queue.id), str(parent.id), 0, user_id)
        db.flush()
        with pytest.raises(SystemBucketError):
            svc.delete_bucket(db, user_id, str(parent.id))

        svc.move_bucket(db, str(queue.id), None, 0, user_id)
        db.flush()
        assert svc.delete_bucket(db, user_id, str(parent.id)) is True
        assert db.get(ReviewBucket, queue.id) is not None

    def test_an_unrelated_parent_still_deletes(self, db, svc, user_id):
        # The user HAS a system bucket; it just isn't under this tree. Deleting must work,
        # or the guard would make every bucket undeletable for anyone with a queue.
        svc.get_or_create_playback_bucket(db, user_id)
        parent = svc.create_bucket(db, user_id, name="일반")
        child = svc.create_bucket(db, user_id, name="자식")
        svc.move_bucket(db, str(child.id), str(parent.id), 0, user_id)
        db.flush()
        # Capture ids BEFORE the delete: `child` goes away via the DB-level parent_id
        # CASCADE, which the ORM session never sees, so reading child.id afterwards would
        # trigger a refresh of a deleted row (ObjectDeletedError) instead of a clean None.
        parent_id, child_id = str(parent.id), child.id

        assert svc.delete_bucket(db, user_id, parent_id) is True
        assert db.get(ReviewBucket, child_id) is None

    def test_another_users_system_bucket_does_not_block_my_delete(self, db, svc, user_id):
        # The subtree query is user-scoped; a stray cross-user match would be a denial bug.
        other = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
        db.execute(
            text(
                "INSERT INTO users (id, handle, display_name) VALUES (:id, :h, :d) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(other), "h": "test-bucketsvc-3", "d": "Test BucketSvc 3"},
        )
        db.flush()
        svc.get_or_create_playback_bucket(db, other)

        parent = svc.create_bucket(db, user_id, name="일반")
        assert svc.delete_bucket(db, user_id, str(parent.id)) is True


class TestSpotifyLibraryManualAddGuard:
    """External-review follow-up to BUG-20 (front #355): `isManualAddTarget()` was the ONLY
    thing stopping a manual add into the sync-owned `kind='spotify_library'` bucket —
    `_assert_item_type_allowed` keys on `bucket.type`, never `kind`, so a direct API call
    bypassed it entirely on every one of add_item's four sibling entry points. All four
    covered here; before this guard, all four succeeded.
    """

    def _spotify_library_bucket(self, db, user_id):
        b = ReviewBucket(
            user_id=user_id, name="spotify_library", kind="spotify_library", position=50
        )
        db.add(b)
        db.flush()
        return b

    def test_add_item_rejected(self, db, svc, album_ids, user_id):
        b = self._spotify_library_bucket(db, user_id)
        with pytest.raises(SystemBucketError):
            svc.add_item(db, user_id, str(b.id), item_type="album", album_id=album_ids[0])
        assert (
            db.query(ReviewBucketItem).filter(ReviewBucketItem.bucket_id == b.id).count() == 0
        )

    def test_ordinary_bucket_add_still_works(self, db, svc, album_ids, user_id):
        # The guard must be spotify_library-specific, not a blanket regression.
        b = svc.create_bucket(db, user_id, name="일반")
        item = svc.add_item(db, user_id, str(b.id), album_id=album_ids[0])
        assert item is not None

    def test_playback_queue_add_still_works(self, db, svc, user_id):
        # SYSTEM_BUCKET_KINDS has three members; only spotify_library is add-restricted —
        # playback_queue accepts manual queue-adds by product design (drag-to-queue).
        album = _mk_album(db)
        t = _mk_track(db, album, "q", 1)
        b = svc.get_or_create_playback_bucket(db, user_id)
        item = svc.add_item(db, user_id, str(b.id), item_type="playback", track_id=str(t.id))
        assert item is not None

    def test_expand_artist_source_rejected(self, db, svc, user_id):
        b = self._spotify_library_bucket(db, user_id)
        artist = _mk_artist(db, "Blocked")
        album = _mk_album_with_artists(db, [artist])
        with pytest.raises(SystemBucketError):
            svc.expand_artist_source(db, user_id, str(b.id), source_album_id=str(album.id))
        assert (
            db.query(ReviewBucketItem).filter(ReviewBucketItem.bucket_id == b.id).count() == 0
        )

    def test_expand_album_tracks_rejected(self, db, svc, user_id):
        b = self._spotify_library_bucket(db, user_id)
        album = _mk_album(db)
        _mk_track(db, album, "t", 1)
        with pytest.raises(SystemBucketError):
            svc.expand_album_tracks(db, user_id, str(b.id), source_album_id=str(album.id))
        assert (
            db.query(ReviewBucketItem).filter(ReviewBucketItem.bucket_id == b.id).count() == 0
        )

    def test_reorder_move_in_rejected(self, db, svc, album_ids, user_id):
        # The drag/move path is a second "add" this guard must also cover.
        src = svc.create_bucket(db, user_id, name="from")
        dst = self._spotify_library_bucket(db, user_id)
        item = svc.add_item(db, user_id, str(src.id), album_id=album_ids[0])
        with pytest.raises(SystemBucketError):
            svc.reorder(db, user_id, [{"id": str(dst.id), "item_ids": [str(item.id)]}])
        # Rejected atomically — the item never actually moved.
        assert str(db.get(ReviewBucketItem, item.id).bucket_id) == str(src.id)

    def test_reorder_within_spotify_library_still_allowed(self, db, svc, album_ids, user_id):
        # Reordering items ALREADY resident there (not a move-in) must not false-positive.
        b = self._spotify_library_bucket(db, user_id)
        i0 = ReviewBucketItem(
            bucket_id=b.id, item_type="album", album_id=album_ids[0], position=0
        )
        i1 = ReviewBucketItem(
            bucket_id=b.id, item_type="album", album_id=album_ids[1], position=1
        )
        db.add_all([i0, i1])
        db.flush()
        svc.reorder(db, user_id, [{"id": str(b.id), "item_ids": [str(i1.id), str(i0.id)]}])
        assert db.get(ReviewBucketItem, i0.id).position == 1
        assert db.get(ReviewBucketItem, i1.id).position == 0


class TestPlaybackTypeGate:
    def test_album_row_rejected_on_the_single_row_path(self, db, svc, album_ids, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        with pytest.raises(BucketTypeError):
            svc.add_item(db, user_id, str(b.id), item_type="album", album_id=album_ids[0])

    def test_artist_row_rejected(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        artist = _mk_artist(db, "Queue Reject")
        with pytest.raises(BucketTypeError):
            svc.add_item(db, user_id, str(b.id), item_type="artist", artist_id=str(artist.id))

    def test_playback_row_accepted(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db)
        trk = _mk_track(db, album, "Only", 1)
        item = svc.add_item(
            db, user_id, str(b.id), item_type="playback", track_id=str(trk.id)
        )
        assert item.item_type == "playback"
        assert str(item.track_id) == str(trk.id)

    def test_duplicate_playback_rows_allowed(self, db, svc, user_id):
        # D8: the queue deliberately has no unique index on item_type='playback',
        # so queueing the same track twice is two rows, not a 409.
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db)
        trk = _mk_track(db, album, "Twice", 1)
        svc.add_item(db, user_id, str(b.id), item_type="playback", track_id=str(trk.id))
        svc.add_item(db, user_id, str(b.id), item_type="playback", track_id=str(trk.id))
        assert len(_queue_track_ids(db, b.id)) == 2

    def test_playback_row_accepts_spotify_track_id(self, db, svc, user_id):
        # ARCH-entity-interaction-v2 Step 5 — a source with no internal Track row
        # reference (the liked-tracks mirror) sends the Spotify id instead of our
        # UUID PK. `Track.id ==` on a non-UUID string used to be an unhandled DB
        # error (500); this must resolve via `Track.spotify_id` instead and store
        # OUR id on the membership row.
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db)
        trk = _mk_track(db, album, "By Spotify Id", 1)
        item = svc.add_item(
            db, user_id, str(b.id), item_type="playback", track_id=trk.spotify_id
        )
        assert item.item_type == "playback"
        assert str(item.track_id) == str(trk.id)

    def test_unknown_track_id_raises_not_found_not_a_db_error(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        with pytest.raises(TrackNotFoundError):
            svc.add_item(
                db, user_id, str(b.id), item_type="playback", track_id="not-a-real-id"
            )


class TestExpandAlbumTracks:
    def test_appends_in_album_order_not_insertion_order(self, db, svc, user_id):
        """The assertion this step exists to make.

        Tracks are INSERTED deliberately scrambled (3, 1, 4, 2) so that any
        implementation relying on insertion order, PK order, or an unordered
        SELECT produces a different sequence than track_no order. If the ORDER BY
        were dropped this test fails; a mocked session could not catch that.
        """
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db)
        t3 = _mk_track(db, album, "third", 3)
        t1 = _mk_track(db, album, "first", 1)
        t4 = _mk_track(db, album, "fourth", 4)
        t2 = _mk_track(db, album, "second", 2)

        added = svc.expand_album_tracks(
            db, user_id, str(b.id), source_album_id=str(album.id)
        )

        expected = [str(t1.id), str(t2.id), str(t3.id), str(t4.id)]
        # Returned order …
        assert [str(t.id) for t in added] == expected
        # … and persisted position order agree.
        assert [tid for _pos, tid in _queue_track_ids(db, b.id)] == expected
        assert [pos for pos, _tid in _queue_track_ids(db, b.id)] == [0, 1, 2, 3]

    def test_null_track_no_sorts_last(self, db, svc, user_id):
        # track_no is nullable; an unnumbered track must not sort to the front and
        # displace a real track 1 (ORDER BY … NULLS LAST).
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db)
        t_null = _mk_track(db, album, "untitled", None)
        t1 = _mk_track(db, album, "first", 1)
        added = svc.expand_album_tracks(
            db, user_id, str(b.id), source_album_id=str(album.id)
        )
        assert [str(t.id) for t in added] == [str(t1.id), str(t_null.id)]

    def test_appends_after_existing_queue_rows(self, db, svc, user_id):
        # Expansion appends to the tail — it never renumbers or displaces what is
        # already queued (and possibly playing).
        b = svc.get_or_create_playback_bucket(db, user_id)
        first_album = _mk_album(db, "Already")
        sitting = _mk_track(db, first_album, "sitting", 1)
        svc.add_item(db, user_id, str(b.id), item_type="playback", track_id=str(sitting.id))

        album = _mk_album(db, "Dropped")
        t1 = _mk_track(db, album, "a", 1)
        t2 = _mk_track(db, album, "b", 2)
        svc.expand_album_tracks(db, user_id, str(b.id), source_album_id=str(album.id))

        assert [tid for _pos, tid in _queue_track_ids(db, b.id)] == [
            str(sitting.id),
            str(t1.id),
            str(t2.id),
        ]

    def test_rows_are_playback_kind_carrying_track_id(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db)
        _mk_track(db, album, "a", 1)
        svc.expand_album_tracks(db, user_id, str(b.id), source_album_id=str(album.id))
        rows = (
            db.query(ReviewBucketItem)
            .filter(ReviewBucketItem.bucket_id == b.id)
            .all()
        )
        assert rows and all(r.item_type == "playback" for r in rows)
        assert all(r.track_id is not None and r.album_id is None for r in rows)

    def test_redrop_duplicates_rather_than_dedupes(self, db, svc, user_id):
        # Unlike expand_artist_source (which skips), re-dropping an album queues it
        # again — the queue allows duplicates by design.
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db)
        _mk_track(db, album, "a", 1)
        _mk_track(db, album, "b", 2)
        svc.expand_album_tracks(db, user_id, str(b.id), source_album_id=str(album.id))
        svc.expand_album_tracks(db, user_id, str(b.id), source_album_id=str(album.id))
        assert len(_queue_track_ids(db, b.id)) == 4

    def test_album_with_no_tracks_is_a_noop(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        album = _mk_album(db, "Unsynced")
        assert svc.expand_album_tracks(
            db, user_id, str(b.id), source_album_id=str(album.id)
        ) == []
        assert _queue_track_ids(db, b.id) == []

    def test_unknown_album_raises(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        with pytest.raises(AlbumNotFoundError):
            svc.expand_album_tracks(
                db, user_id, str(b.id), source_album_id=str(uuid.uuid4())
            )

    def test_missing_bucket_raises(self, db, svc, user_id):
        album = _mk_album(db)
        with pytest.raises(BucketNotFoundError):
            svc.expand_album_tracks(
                db, user_id, str(uuid.uuid4()), source_album_id=str(album.id)
            )

    def test_rejected_on_an_artist_bucket(self, db, svc, user_id):
        # Expansion produces playback rows, so it must clear the same type gate the
        # single-row path clears.
        b = svc.create_bucket(db, user_id, name="아티스트", type="artist")
        album = _mk_album(db)
        _mk_track(db, album, "a", 1)
        with pytest.raises(BucketTypeError):
            svc.expand_album_tracks(
                db, user_id, str(b.id), source_album_id=str(album.id)
            )

    def test_works_on_a_general_bucket(self, db, svc, user_id):
        # A General bucket accepts every kind (today's behaviour), so an album drop
        # expands there too — the gate is on Artist/Playback buckets, not on this method.
        b = svc.create_bucket(db, user_id, name="일반")
        album = _mk_album(db)
        _mk_track(db, album, "a", 1)
        added = svc.expand_album_tracks(
            db, user_id, str(b.id), source_album_id=str(album.id)
        )
        assert len(added) == 1


class TestListBucketsTrackCoverEagerLoad:
    """ARCH-global-playback-experience Step 3: TrackBrief.cover_url is resolved off
    track.album.cover_url (app/api/routes/buckets.py's `_track_brief`). list_buckets'
    selectinload options must eager-load ReviewBucketItem.track → Track.album for this,
    or a playback-row queue N+1s one SELECT per row the first time `_track_brief` reads
    `.album` — a mocked service test can't see this, only a real session's query log can."""

    def test_track_album_accessible_without_per_row_query(self, db, svc, user_id):
        b = svc.get_or_create_playback_bucket(db, user_id)
        album1 = _mk_album(db, "Cover A")
        album1.cover_url = "https://cdn/cover-a.jpg"
        album2 = _mk_album(db, "Cover B")
        album2.cover_url = "https://cdn/cover-b.jpg"
        db.flush()
        t1 = _mk_track(db, album1, "one", 1)
        t2 = _mk_track(db, album2, "two", 1)
        svc.add_item(db, user_id, str(b.id), item_type="playback", track_id=str(t1.id))
        svc.add_item(db, user_id, str(b.id), item_type="playback", track_id=str(t2.id))

        from sqlalchemy import event

        queries = []
        listener = lambda conn, cursor, statement, *a: queries.append(statement)
        event.listen(db.get_bind(), "before_cursor_execute", listener)
        try:
            roots = svc.list_buckets(db, user_id)
            queue = next(r for r in roots if str(r.id) == str(b.id))
            # The access itself must not add a query — it should already be populated.
            n_before = len(queries)
            covers = {str(it.track.id): it.track.album.cover_url for it in queue.items}
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", listener)

        assert covers == {
            str(t1.id): "https://cdn/cover-a.jpg",
            str(t2.id): "https://cdn/cover-b.jpg",
        }
        # Reading .track.album on every row after list_buckets() returned must not have
        # issued any further SELECTs — proves the eager-load, not a lazy per-row fetch.
        assert len(queries) == n_before

    def test_extra_select_count_does_not_scale_with_row_count(self, db, svc, user_id):
        # Compares the query count list_buckets() itself issues before vs. after adding
        # more playback rows across distinct albums, on the SAME db session/bucket (the
        # two calls below are cumulative — 1 row, then +4 more for 5 total, not two
        # independent 1-row/4-row buckets) — a per-row lazy load would make the count
        # grow with row count; the eager-loaded case does not.
        from sqlalchemy import event

        def _list_buckets_query_count(n_new_rows):
            b = svc.get_or_create_playback_bucket(db, user_id)
            for i in range(n_new_rows):
                album = _mk_album(db, f"Scale {i}")
                trk = _mk_track(db, album, f"t{i}", 1)
                svc.add_item(
                    db, user_id, str(b.id), item_type="playback", track_id=str(trk.id)
                )
            queries = []
            listener = lambda conn, cursor, statement, *a: queries.append(statement)
            event.listen(db.get_bind(), "before_cursor_execute", listener)
            try:
                roots = svc.list_buckets(db, user_id)
                queue = next(r for r in roots if str(r.id) == str(b.id))
                for it in queue.items:
                    _ = it.track.album.cover_url if it.track else None
            finally:
                event.remove(db.get_bind(), "before_cursor_execute", listener)
            return len(queries)

        n_at_one_row = _list_buckets_query_count(1)
        n_at_five_rows = _list_buckets_query_count(4)  # cumulative: 1 + 4 = 5 rows now
        assert n_at_one_row == n_at_five_rows


class TestSystemBucketPublishGuard:
    @pytest.mark.parametrize("kind", ["playback_queue", "spotify_library", "to_listen"])
    def test_system_bucket_cannot_be_published(self, db, svc, user_id, kind):
        b = ReviewBucket(user_id=user_id, name=kind, kind=kind, position=60)
        db.add(b)
        db.flush()
        with pytest.raises(ValueError):
            svc.update_bucket(db, user_id, str(b.id), is_public=True)


class TestPlaybackBucketCreateGuard:
    def test_user_cannot_create_a_playback_typed_bucket(self, db, svc, user_id):
        # A user-minted type='playback' bucket would have kind='review': outside the
        # singleton index and outside the delete guard. Rejected at the service.
        with pytest.raises(ValueError):
            svc.create_bucket(db, user_id, name="가짜 대기열", type="playback")
