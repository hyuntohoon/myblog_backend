"""Real-Postgres coverage for tracked-artist SQL and ownership isolation."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.integration.catalog import seed_catalog

from app.services.bucket_service import BucketNotFoundError, BucketService
from app.services.tracked_artist_service import (
    ArtistNotFoundError,
    TrackedArtistRateLimitError,
    TrackedArtistService,
)
from myblog_shared_db.models import ReviewBucket, ReviewBucketItem

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]

MEMBER_A = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
MEMBER_B = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    outer = conn.begin()
    Session = sessionmaker(bind=conn, join_transaction_mode="create_savepoint")
    session = Session()
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def artist_ids(db):
    # OPS-integration-db-locality Step 1 — seeded into this test's transaction
    # instead of borrowed from ambient rows. The fixture catalog gives
    # artist_ids[0] an album_artists credit, which the bucket-preview test below
    # needs and used to skip without. UUID objects, as before.
    return [uuid.UUID(a) for a in seed_catalog(db).artist_ids]


@pytest.fixture(autouse=True)
def users(db):
    db.execute(
        text(
            "INSERT INTO users (id, handle, display_name) VALUES "
            "(:a, :ha, 'Track A'), (:b, :hb, 'Track B') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "a": str(MEMBER_A),
            "ha": f"tracked-a-{uuid.uuid4().hex[:8]}",
            "b": str(MEMBER_B),
            "hb": f"tracked-b-{uuid.uuid4().hex[:8]}",
        },
    )
    db.flush()


def test_bulk_upsert_is_idempotent_and_member_scoped(db, artist_ids):
    svc = TrackedArtistService()

    assert svc.add_tracks(
        db, MEMBER_A, reversed(artist_ids[:2]), daily_cap=10
    ) == (2, 0)
    assert svc.add_tracks(db, MEMBER_A, artist_ids[:2], daily_cap=10) == (0, 2)
    assert [row for row in svc.list_tracks(db, MEMBER_B)] == []
    assert len(svc.list_tracks(db, MEMBER_A)) == 2

    assert svc.delete_track(db, MEMBER_B, artist_ids[0]) is False
    assert len(svc.list_tracks(db, MEMBER_A)) == 2
    assert svc.delete_track(db, MEMBER_A, artist_ids[0]) is True
    assert len(svc.list_tracks(db, MEMBER_A)) == 1


def test_unknown_artist_rejects_whole_batch(db, artist_ids):
    svc = TrackedArtistService()
    unknown = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

    with pytest.raises(ArtistNotFoundError):
        svc.add_tracks(db, MEMBER_A, [artist_ids[0], unknown], daily_cap=10)

    assert svc.list_tracks(db, MEMBER_A) == []


def test_rolling_cap_counts_only_new_edges(db, artist_ids):
    svc = TrackedArtistService()
    assert svc.add_tracks(db, MEMBER_A, [artist_ids[0]], daily_cap=1) == (1, 0)
    assert svc.add_tracks(db, MEMBER_A, [artist_ids[0]], daily_cap=1) == (0, 1)

    with pytest.raises(TrackedArtistRateLimitError):
        svc.add_tracks(db, MEMBER_A, [artist_ids[1]], daily_cap=1)


def test_bucket_preview_expands_album_and_hides_cross_member_bucket(db, artist_ids):
    bucket = ReviewBucket(user_id=MEMBER_A, name="Import", position=0)
    db.add(bucket)
    db.flush()
    # The credit is seeded (fixture album 1 ⟵ fixture artist 1), so resolving it
    # is now an assertion rather than a skip: a missing credit means the catalog
    # fixture regressed, which must fail loudly.
    album_id = db.execute(
        text(
            "SELECT aa.album_id FROM album_artists aa "
            "WHERE aa.artist_id = :artist_id LIMIT 1"
        ),
        {"artist_id": str(artist_ids[0])},
    ).scalar_one_or_none()
    assert album_id is not None, "seeded catalog must credit artist_ids[0] on an album"
    db.add(
        ReviewBucketItem(
            bucket_id=bucket.id,
            item_type="album",
            album_id=album_id,
            position=0,
        )
    )
    db.flush()

    artists = BucketService().bucket_catalog_artists(db, MEMBER_A, bucket.id)
    assert artist_ids[0] in {artist.id for artist in artists}
    with pytest.raises(BucketNotFoundError):
        BucketService().bucket_catalog_artists(db, MEMBER_B, bucket.id)


# ── FEAT-lyrics-listening-experience Step 5 — provenance and exclusions ──────
#
# From Step 5 the worker reconciles Spotify follows into this same table every 15
# minutes, which changes what these two routes mean: an add has to record WHY the
# edge exists, and a delete has to say something the next reconcile will respect.
# Both are SQL properties (a composite FK, an ON CONFLICT, one transaction), so they
# are proved here against real Postgres rather than against a mock.

def _origins(db, user_id, artist_id):
    return set(db.execute(
        text("SELECT origin FROM user_artist_track_origins "
             "WHERE user_id = :u AND artist_id = :a"),
        {"u": str(user_id), "a": str(artist_id)},
    ).scalars())


def _excluded(db, user_id, artist_id):
    return db.execute(
        text("SELECT 1 FROM user_artist_follow_exclusions "
             "WHERE user_id = :u AND artist_id = :a"),
        {"u": str(user_id), "a": str(artist_id)},
    ).first() is not None


def test_adding_an_artist_records_manual_provenance(db, artist_ids):
    svc = TrackedArtistService()
    svc.add_tracks(db, MEMBER_A, [artist_ids[0]])

    assert _origins(db, MEMBER_A, artist_ids[0]) == {"manual"}
    # Member B is the control: provenance is member-scoped like the edge itself.
    assert _origins(db, MEMBER_B, artist_ids[0]) == set()


def test_adding_an_artist_already_followed_on_spotify_adds_manual_alongside_it(
        db, artist_ids):
    """The union in OQ2. Without this the later unfollow would take the edge with it."""
    svc = TrackedArtistService()
    db.execute(
        text("INSERT INTO user_artist_tracks (user_id, artist_id) VALUES (:u, :a)"),
        {"u": str(MEMBER_A), "a": str(artist_ids[0])},
    )
    db.execute(
        text("INSERT INTO user_artist_track_origins (user_id, artist_id, origin) "
             "VALUES (:u, :a, 'spotify_follow')"),
        {"u": str(MEMBER_A), "a": str(artist_ids[0])},
    )

    added, already = svc.add_tracks(db, MEMBER_A, [artist_ids[0]])

    assert (added, already) == (0, 1), "the edge already existed"
    assert _origins(db, MEMBER_A, artist_ids[0]) == {"manual", "spotify_follow"}


def test_deleting_a_tracked_artist_records_an_exclusion(db, artist_ids):
    """Without the exclusion, a removal is a 15-minute pause: the worker's next
    reconcile still sees the follow at the provider and re-imports the artist."""
    svc = TrackedArtistService()
    svc.add_tracks(db, MEMBER_A, [artist_ids[0], artist_ids[1]])

    assert svc.delete_track(db, MEMBER_A, artist_ids[0]) is True

    assert _excluded(db, MEMBER_A, artist_ids[0])
    # The artist they kept is the control — a delete must not exclude the batch.
    assert not _excluded(db, MEMBER_A, artist_ids[1])
    # Provenance cascades with the edge (V58's composite FK).
    assert _origins(db, MEMBER_A, artist_ids[0]) == set()
    assert _origins(db, MEMBER_A, artist_ids[1]) == {"manual"}


def test_deleting_an_untracked_artist_records_nothing(db, artist_ids):
    svc = TrackedArtistService()

    assert svc.delete_track(db, MEMBER_A, artist_ids[0]) is False

    assert not _excluded(db, MEMBER_A, artist_ids[0]), (
        "a no-op delete must not create a fence the member never asked for"
    )


def test_adding_an_excluded_artist_back_clears_the_exclusion(db, artist_ids):
    """The 'until cleared' half of OQ2: the fence is against automatic re-import,
    not against the member changing their mind."""
    svc = TrackedArtistService()
    svc.add_tracks(db, MEMBER_A, [artist_ids[0]])
    svc.delete_track(db, MEMBER_A, artist_ids[0])
    assert _excluded(db, MEMBER_A, artist_ids[0])

    svc.add_tracks(db, MEMBER_A, [artist_ids[0]])

    assert not _excluded(db, MEMBER_A, artist_ids[0])
    assert _origins(db, MEMBER_A, artist_ids[0]) == {"manual"}


def test_one_members_exclusion_does_not_reach_another_member(db, artist_ids):
    svc = TrackedArtistService()
    svc.add_tracks(db, MEMBER_A, [artist_ids[0]])
    svc.add_tracks(db, MEMBER_B, [artist_ids[0]])

    svc.delete_track(db, MEMBER_A, artist_ids[0])

    assert _excluded(db, MEMBER_A, artist_ids[0])
    assert not _excluded(db, MEMBER_B, artist_ids[0])
    assert _origins(db, MEMBER_B, artist_ids[0]) == {"manual"}
