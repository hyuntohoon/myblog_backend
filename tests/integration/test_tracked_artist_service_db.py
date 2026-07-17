"""Real-Postgres coverage for tracked-artist SQL and ownership isolation."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.bucket_service import BucketNotFoundError, BucketService
from app.services.tracked_artist_service import (
    ArtistNotFoundError,
    TrackedArtistRateLimitError,
    TrackedArtistService,
)
from myblog_shared_db.models import ReviewBucket, ReviewBucketItem

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"
)

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
    ids = [row[0] for row in db.execute(text("SELECT id FROM artists LIMIT 3")).all()]
    if len(ids) < 2:
        pytest.skip("need >=2 artists in test DB")
    return ids


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
    album_id = db.execute(
        text(
            "SELECT aa.album_id FROM album_artists aa "
            "WHERE aa.artist_id = :artist_id LIMIT 1"
        ),
        {"artist_id": str(artist_ids[0])},
    ).scalar_one_or_none()
    if album_id is None:
        pytest.skip("need an album credit for test artist")
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
