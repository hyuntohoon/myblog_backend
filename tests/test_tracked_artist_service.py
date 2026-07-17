from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from app.services.bucket_service import BucketService
from app.services.distribution import VARIOUS_ARTISTS
from app.services.tracked_artist_service import TrackedArtistService

MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
BUCKET_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
ARTIST_A = uuid.UUID("10000000-0000-0000-0000-000000000001")
ARTIST_B = uuid.UUID("10000000-0000-0000-0000-000000000002")
ARTIST_C = uuid.UUID("10000000-0000-0000-0000-000000000003")


def _artist(artist_id, name):
    return SimpleNamespace(id=artist_id, name=name, photo_url=None)


def test_bucket_candidates_expand_distinct_structured_credits_and_direct_artist():
    artist_a = _artist(ARTIST_A, "A")
    artist_b = _artist(ARTIST_B, "B")
    artist_c = _artist(ARTIST_C, "C")
    various = _artist(uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"), VARIOUS_ARTISTS)
    bucket = SimpleNamespace(
        items=[
            SimpleNamespace(
                item_type="album",
                album=SimpleNamespace(artists=[artist_b, various, artist_a, artist_b]),
            ),
            SimpleNamespace(
                item_type="track",
                track=SimpleNamespace(artists=[artist_c, artist_b]),
            ),
            SimpleNamespace(item_type="artist", artist=artist_c),
        ]
    )
    db = MagicMock()
    db.query.return_value.options.return_value.filter.return_value.first.return_value = (
        bucket
    )

    artists = BucketService().bucket_catalog_artists(db, MEMBER_ID, BUCKET_ID)

    assert [artist.id for artist in artists] == [ARTIST_A, ARTIST_B, ARTIST_C]


def test_bulk_insert_values_are_sorted_by_user_artist_conflict_key():
    class ScalarRows:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            rows = self.rows

            class _Scalars:
                def __iter__(self):
                    return iter(rows)

                def all(self):
                    return list(rows)

            return _Scalars()

    # The insert result is consumed via RETURNING scalars (not rowcount —
    # prod psycopg reported -1 for this statement shape).
    insert_result = ScalarRows([ARTIST_A, ARTIST_B, ARTIST_C])
    db = MagicMock()
    db.execute.side_effect = [
        ScalarRows([ARTIST_A, ARTIST_B, ARTIST_C]),
        ScalarRows([]),
        insert_result,
    ]
    db.scalar.return_value = 0

    result = TrackedArtistService().add_tracks(
        db,
        MEMBER_ID,
        [ARTIST_C, ARTIST_A, ARTIST_B, ARTIST_A],
        daily_cap=500,
    )

    statement = db.execute.call_args_list[2].args[0]
    params = statement.compile(dialect=postgresql.dialect()).params
    inserted_ids = [params[f"artist_id_m{i}"] for i in range(3)]
    assert inserted_ids == [ARTIST_A, ARTIST_B, ARTIST_C]
    assert result == (3, 0)
    db.commit.assert_called_once_with()
