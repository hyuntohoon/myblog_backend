"""Real-engine integration tests for PlaybackService.resolve_uri's `provider`
argument (FEAT-youtube-playback-provider Step A1).

Why a real engine rather than the MagicMock db the sibling unit tests use: the
entire YouTube branch IS a WHERE clause. A mock returns whatever it was told to
regardless of the filter, so `verify_state = 'live'` and `embeddable IS NOT
FALSE` would pass a mocked test even if they were deleted from the query — which
is exactly the mutant this file has to fail on
(feedback-mutation-test-your-own-new-tests, feedback-sa-session-lifecycle-mock-blind).

The control test lives here too, and it is the load-bearing one: OMITTING
`provider` must still return the Spotify URI. Without it this suite could pass
while the default silently changed and every shipped caller broke.

Gated on TEST_DB_URL (Neon test branch). Each test runs inside an outer
transaction rolled back on teardown — nothing persists.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.integration.catalog import seed_catalog

from app.services.playback_service import PlaybackItemNotFoundError, PlaybackService

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]


#: The constraints V55 is responsible for. Named here so a missing one is a
#: NAMED failure ("ck_tpr_provider absent") rather than a mystery INSERT that
#: unexpectedly succeeded three tests later.
_REQUIRED_CONSTRAINTS = {
    "ck_tpr_provider",
    "ck_tpr_kind",
    "ck_tpr_source",
    "ck_tpr_verify_state",
    "uq_tpr_track_provider",
}


@pytest.fixture(scope="module")
def engine():
    """Assert the schema under test came from V55. Deliberately does NOT create
    the table.

    An earlier draft opened with `CREATE TABLE IF NOT EXISTS <the whole DDL>` to
    "guard against a lagging test branch", copying the sibling integration files.
    That is wrong here and it was wrong for the reason this file exists: it makes
    the suite green whether or not V55 was applied or correct, because the table
    the tests exercise is the one the test file just created. `IF NOT EXISTS`
    makes it worse — a pre-existing table with DIFFERENT columns silently wins
    and nothing reports the divergence. A real engine running against a schema
    the test authored is a mock with extra steps.

    So: fail loudly instead. CI loads the canonical schema from the pinned
    shared-db commit, so a red here means the pin was not bumped — which is
    exactly the failure worth being told about.
    """
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        exists = conn.execute(
            text("SELECT to_regclass('public.track_provider_refs') IS NOT NULL")
        ).scalar_one()
        assert exists, (
            "track_provider_refs is absent from the test database. V55 has not "
            "been applied here, or the shared-db pin used to load the canonical "
            "schema predates it."
        )
        found = set(
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'track_provider_refs'::regclass"
                )
            ).scalars()
        )
        missing = _REQUIRED_CONSTRAINTS - found
        assert not missing, f"V55 constraints missing from the test database: {sorted(missing)}"
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
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
def catalog(db):
    return seed_catalog(db)


@pytest.fixture
def svc():
    return PlaybackService()


def _map(db, track_id, video_id, *, verify_state="live", embeddable=True):
    db.execute(
        text("""
            INSERT INTO track_provider_refs
              (track_id, provider, external_id, external_kind, source,
               embeddable, verify_state)
            VALUES
              (:tid, 'youtube', :vid, 'video', 'user_confirmed', :emb, :vs)
        """),
        {"tid": track_id, "vid": video_id, "emb": embeddable, "vs": verify_state},
    )


class TestSpotifyRemainsTheDefault:
    """The control. If these fail, the step broke every shipped caller."""

    def test_omitting_provider_still_returns_the_spotify_uri(self, db, svc, catalog):
        """THE control test the RFC names: no `provider` argument at all."""
        track_id = catalog.track_ids[0]
        spotify_id = db.execute(
            text("SELECT spotify_id FROM tracks WHERE id = :i"), {"i": track_id}
        ).scalar_one()

        uri = svc.resolve_uri(db, item_type="track", item_id=track_id)

        assert uri == f"spotify:track:{spotify_id}"

    def test_a_youtube_mapping_does_not_change_the_default(self, db, svc, catalog):
        """A mapped track must STILL resolve to Spotify when provider is omitted —
        the provider is a property of the play attempt, not of the track."""
        track_id = catalog.track_ids[0]
        _map(db, track_id, "yt-should-not-win")
        spotify_id = db.execute(
            text("SELECT spotify_id FROM tracks WHERE id = :i"), {"i": track_id}
        ).scalar_one()

        assert svc.resolve_uri(db, item_type="track", item_id=track_id) == (
            f"spotify:track:{spotify_id}"
        )

    def test_album_still_resolves_to_a_spotify_context_uri(self, db, svc, catalog):
        album_id = catalog.album_ids[0]
        spotify_id = db.execute(
            text("SELECT spotify_id FROM albums WHERE id = :i"), {"i": album_id}
        ).scalar_one()

        assert svc.resolve_uri(db, item_type="album", item_id=album_id) == (
            f"spotify:album:{spotify_id}"
        )


class TestYouTubeResolution:
    def test_mapped_live_track_resolves_to_the_video_uri(self, db, svc, catalog):
        track_id = catalog.track_ids[0]
        _map(db, track_id, "dQw4w9WgXcQ")

        uri = svc.resolve_uri(
            db, item_type="track", item_id=track_id, provider="youtube"
        )

        assert uri == "youtube:video:dQw4w9WgXcQ"

    def test_unmapped_track_is_a_404(self, db, svc, catalog):
        with pytest.raises(PlaybackItemNotFoundError):
            svc.resolve_uri(
                db, item_type="track", item_id=catalog.track_ids[0], provider="youtube"
            )

    def test_the_mapping_is_per_track_not_global(self, db, svc, catalog):
        """Mapping track A must not resolve track B — proves the track_id filter
        is real, which a mock returning a fixed row could never show."""
        a, b = catalog.track_ids[0], catalog.track_ids[1]
        _map(db, a, "video-for-a")

        assert svc.resolve_uri(
            db, item_type="track", item_id=a, provider="youtube"
        ) == "youtube:video:video-for-a"
        with pytest.raises(PlaybackItemNotFoundError):
            svc.resolve_uri(db, item_type="track", item_id=b, provider="youtube")

    def test_album_on_youtube_is_a_404_not_a_guess(self, db, svc, catalog):
        """YouTube has no album-context equivalent of a Spotify context_uri.

        Asserts the MESSAGE, not just the exception. An album id has no row in
        track_provider_refs either, so deleting the `item_type != "track"` guard
        would still raise — this test would pass while the guard was gone. The
        guard raises 'youtube:album:<id>'; the fall-through raises
        'youtube:track:<id>'. Only the message tells them apart.
        """
        album_id = catalog.album_ids[0]
        with pytest.raises(PlaybackItemNotFoundError) as exc:
            svc.resolve_uri(
                db, item_type="album", item_id=album_id, provider="youtube"
            )
        assert str(exc.value) == f"youtube:album:{album_id}"

    def test_malformed_id_is_a_404(self, db, svc):
        with pytest.raises(PlaybackItemNotFoundError):
            svc.resolve_uri(
                db, item_type="track", item_id="not-a-uuid", provider="youtube"
            )


class TestUnplayableMappingsDoNotResolve:
    """These are the tests that die if the WHERE clause is weakened. Each one
    passes trivially against a mock; only a real engine runs the filter."""

    @pytest.mark.parametrize("state", ["gone", "not_embeddable"])
    def test_a_non_live_mapping_is_a_404(self, db, svc, catalog, state):
        """Handing the IFrame player an id the refresh job already marked dead
        turns a clean 'no mapping' into an opaque player error."""
        track_id = catalog.track_ids[0]
        _map(db, track_id, "dead-video", verify_state=state)

        with pytest.raises(PlaybackItemNotFoundError):
            svc.resolve_uri(
                db, item_type="track", item_id=track_id, provider="youtube"
            )

    def test_a_known_non_embeddable_mapping_is_a_404(self, db, svc, catalog):
        track_id = catalog.track_ids[0]
        _map(db, track_id, "no-embed", embeddable=False)

        with pytest.raises(PlaybackItemNotFoundError):
            svc.resolve_uri(
                db, item_type="track", item_id=track_id, provider="youtube"
            )

    def test_embeddable_null_still_resolves(self, db, svc, catalog):
        """NULL means 'videos.list has not checked this yet', which is NOT the
        same as 'known unplayable'. This is why the filter is `IS NOT FALSE`
        rather than `== True` — with `== True` this test fails and a freshly
        confirmed mapping would be unplayable until the first refresh ran."""
        track_id = catalog.track_ids[0]
        _map(db, track_id, "unchecked-yet", embeddable=None)

        assert svc.resolve_uri(
            db, item_type="track", item_id=track_id, provider="youtube"
        ) == "youtube:video:unchecked-yet"


class TestRetentionWindowIsEnforcedAtReadTime:
    """YouTube Developer Policy III.E.4.c/.d — 30 calendar days, no exception for
    a resource id. The Step-A5 sweep is what DELETES expired rows, but it does
    not exist yet and can be down once it does, so resolve refuses expired data
    on its own. These tests are the only thing holding that promise today."""

    def test_a_row_just_inside_the_window_resolves(self, db, svc, catalog):
        track_id = catalog.track_ids[0]
        _map(db, track_id, "fresh-enough")
        db.execute(
            text(
                "UPDATE track_provider_refs SET last_verified_at = now() - interval '29 days'"
                " WHERE track_id = :t"
            ),
            {"t": track_id},
        )

        assert svc.resolve_uri(
            db, item_type="track", item_id=track_id, provider="youtube"
        ) == "youtube:video:fresh-enough"

    def test_a_row_past_thirty_days_does_not_resolve(self, db, svc, catalog):
        """Still verify_state='live' and still embeddable — expired purely on the
        clock. If this passes only because of the other two filters, it is not
        testing retention, so the row is deliberately healthy in every other way."""
        track_id = catalog.track_ids[0]
        _map(db, track_id, "expired-video")
        db.execute(
            text(
                "UPDATE track_provider_refs SET last_verified_at = now() - interval '31 days'"
                " WHERE track_id = :t"
            ),
            {"t": track_id},
        )
        state, emb = db.execute(
            text(
                "SELECT verify_state, embeddable FROM track_provider_refs WHERE track_id = :t"
            ),
            {"t": track_id},
        ).one()
        assert (state, emb) == ("live", True), "row must be healthy apart from its age"

        with pytest.raises(PlaybackItemNotFoundError):
            svc.resolve_uri(
                db, item_type="track", item_id=track_id, provider="youtube"
            )


class TestV55ConstraintsAreReallyEnforced:
    """The constraints had NO committed coverage — they were checked once by hand
    in the session that wrote the migration, which is not a regression test.

    tests/test_schema_parity.py in shared_db cannot cover this either: its
    _extract_columns skips every primary/foreign/unique/check clause and compares
    column NAMES only. A future migration could drop ck_tpr_provider and every
    gate in every repo would stay green while "Spotify can never land in this
    table" quietly stopped being true. This class is that gate.
    """

    def test_spotify_cannot_be_stored_here(self, db, catalog):
        """The single most important property of this table: it must not become a
        second home for tracks.spotify_id."""
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO track_provider_refs"
                    " (track_id, provider, external_id, external_kind, source)"
                    " VALUES (:t, 'spotify', 'x', 'video', 'user_confirmed')"
                ),
                {"t": catalog.track_ids[0]},
            )

    @pytest.mark.parametrize("bad_source", ["search_auto", "playlist_import"])
    def test_only_user_confirmed_is_a_valid_source(self, db, catalog, bad_source):
        """No unconfirmed mapping in v1, and no importer either — the table is
        global, so it cannot model a per-member import."""
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO track_provider_refs"
                    " (track_id, provider, external_id, external_kind, source)"
                    " VALUES (:t, 'youtube', 'x', 'video', :s)"
                ),
                {"t": catalog.track_ids[0], "s": bad_source},
            )

    def test_one_video_per_track(self, db, catalog):
        track_id = catalog.track_ids[0]
        _map(db, track_id, "first-video")
        with pytest.raises(IntegrityError):
            _map(db, track_id, "second-video")

    def test_deleting_the_track_cascades_the_mapping(self, db, catalog):
        track_id = catalog.track_ids[0]
        _map(db, track_id, "doomed")
        db.execute(text("DELETE FROM tracks WHERE id = :t"), {"t": track_id})

        left = db.execute(
            text("SELECT count(*) FROM track_provider_refs WHERE track_id = :t"),
            {"t": track_id},
        ).scalar_one()
        assert left == 0

    def test_verify_state_is_a_closed_set(self, db, catalog):
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO track_provider_refs"
                    " (track_id, provider, external_id, external_kind, source, verify_state)"
                    " VALUES (:t, 'youtube', 'x', 'video', 'user_confirmed', 'probably_fine')"
                ),
                {"t": catalog.track_ids[0]},
            )
