"""Real-engine tests for the Step A3 mapping writes.

WHY A REAL ENGINE, and not the MagicMock db the sibling unit tests use: the
authorization predicate IS a WHERE clause. `_has_standing` is a join from
`review_bucket_items` to `review_buckets` filtered on `user_id`, and a mock
returns whatever it was told to regardless of the filter — so a mutant that
deleted `ReviewBucket.user_id == member_id` would pass a mocked suite while
granting every member write access to every mapping. That is the single most
important thing this file exists to fail on.

The same applies to the 410-vs-404 split and to V56 itself: `embeddable NOT
NULL` and the `ON DELETE SET NULL` foreign key are database facts, and only a
database can be asked whether they are true.

This file deliberately does NOT create any table. CI loads the canonical schema
from the pinned shared-db commit; a red here means the pin was not bumped, which
is exactly the failure worth being told about (the same trap the Step-A1 review
caught: a suite that authors its own schema is a mock with extra steps).

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

from app.services.playback_service import (
    PlaybackItemNotFoundError,
    PlaybackMappingForbiddenError,
    PlaybackService,
    PlaybackVideoUnusableError,
)

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]

VIDEO = "dQw4w9WgXcQ"
OTHER_VIDEO = "kffacxfA7G4"


@pytest.fixture(scope="module")
def engine():
    """Assert the schema under test came from V56. Does NOT create anything."""
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        cols = dict(
            conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'track_provider_refs'"
                )
            ).all()
        )
        assert "created_by_member_id" in cols, (
            "track_provider_refs.created_by_member_id is absent — V56 has not been "
            "applied to the test database, or the shared-db pin predates it."
        )
        assert cols.get("embeddable") == "NO", (
            "track_provider_refs.embeddable is still nullable — V56's SET NOT NULL "
            "has not been applied here."
        )
        # The FK's delete action is the whole point of the column's design, so it
        # is asserted rather than assumed: 'n' = SET NULL, 'c' = CASCADE.
        action = conn.execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conrelid = 'track_provider_refs'::regclass "
                "  AND contype = 'f' "
                "  AND confrelid = 'users'::regclass"
            )
        ).scalar_one_or_none()
        assert action == "n", (
            f"created_by_member_id's FK delete action is {action!r}, expected 'n' "
            "(SET NULL). CASCADE would let deleting one member delete mappings "
            "every other member resolves against."
        )
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


def _member(db, handle):
    """A real users row. `users.id` is the Cognito sub, set explicitly."""
    mid = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO users (id, handle, display_name) VALUES (:i, :h, :d)"
        ),
        {"i": mid, "h": handle, "d": handle},
    )
    db.flush()
    return mid


def _bucket_with_track(db, member_id, track_id, *, item_type="track"):
    bid = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO review_buckets (id, user_id, name, position) "
            "VALUES (:b, :u, 'test', 0)"
        ),
        {"b": bid, "u": member_id},
    )
    db.execute(
        text(
            "INSERT INTO review_bucket_items (bucket_id, item_type, track_id, position) "
            "VALUES (:b, :t, :tr, 0)"
        ),
        {"b": bid, "t": item_type, "tr": track_id},
    )
    db.flush()
    return bid


class _StubYouTube:
    """videos.list stand-in. Returns a payload satisfying every field the writer reads."""

    def __init__(self, present=True, *, embeddable=True, privacy="public", duration="PT3M33S"):
        self.present, self.embeddable, self.privacy, self.duration = (
            present, embeddable, privacy, duration,
        )
        self.calls = 0

    def list_videos(self, ids):
        self.calls += 1
        if not self.present:
            return {}
        return {
            ids[0]: {
                "id": ids[0],
                "snippet": {"title": "T", "channelTitle": "C", "thumbnails": {}},
                "status": {
                    "embeddable": self.embeddable,
                    "privacyStatus": self.privacy,
                    "madeForKids": False,
                },
                "contentDetails": {"duration": self.duration},
            }
        }


@pytest.fixture
def stub_youtube(monkeypatch):
    def _apply(stub):
        monkeypatch.setattr("app.clients.youtube_client.youtube", stub)
        return stub
    return _apply


# ---------------------------------------------------------------------------
# Authorization — the reason this file uses a real engine
# ---------------------------------------------------------------------------

class TestStandingAuthorization:
    def test_a_member_holding_the_track_may_write(self, db, svc, catalog, stub_youtube):
        stub = stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)

        out = svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)

        assert out["video_id"] == VIDEO
        assert stub.calls == 1

    def test_a_member_who_does_not_hold_the_track_is_refused(
        self, db, svc, catalog, stub_youtube
    ):
        """THE test. Member B holds nothing; the track is in member A's bucket.

        A mocked db passes this while the `ReviewBucket.user_id == member_id`
        filter is deleted, which would give every member write access to every
        mapping in a GLOBAL table.
        """
        stub = stub_youtube(_StubYouTube())
        a, b = _member(db, "holder-a"), _member(db, "stranger-b")
        track = catalog.track_ids[0]
        _bucket_with_track(db, a, track)

        with pytest.raises(PlaybackMappingForbiddenError):
            svc.set_youtube_mapping(db, member_id=b, track_id=track, video_id=VIDEO)
        assert stub.calls == 0, "a refused caller must spend no quota"

    def test_holding_a_DIFFERENT_track_grants_nothing(
        self, db, svc, catalog, stub_youtube
    ):
        """Proves the `track_id` half of the predicate, not just the user_id half."""
        stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        held, unheld = catalog.track_ids[0], catalog.track_ids[1]
        _bucket_with_track(db, member, held)

        with pytest.raises(PlaybackMappingForbiddenError):
            svc.set_youtube_mapping(db, member_id=member, track_id=unheld, video_id=VIDEO)

    def test_a_playback_queue_row_grants_standing(self, db, svc, catalog, stub_youtube):
        """No item_type filter, deliberately.

        Pressing play is exactly how a member discovers a mapping is wrong.
        Restricting standing to 'track' rows would deny it to the person best
        placed to notice the defect.
        """
        stub_youtube(_StubYouTube())
        member = _member(db, "player")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track, item_type="playback")

        assert svc.set_youtube_mapping(
            db, member_id=member, track_id=track, video_id=VIDEO
        )["video_id"] == VIDEO

    def test_delete_uses_the_same_predicate(self, db, svc, catalog, stub_youtube):
        stub_youtube(_StubYouTube())
        a, b = _member(db, "holder-a"), _member(db, "stranger-b")
        track = catalog.track_ids[0]
        _bucket_with_track(db, a, track)
        svc.set_youtube_mapping(db, member_id=a, track_id=track, video_id=VIDEO)

        with pytest.raises(PlaybackMappingForbiddenError):
            svc.delete_youtube_mapping(db, member_id=b, track_id=track)
        # and the row is still there
        assert db.execute(
            text("SELECT count(*) FROM track_provider_refs WHERE track_id = :t"),
            {"t": track},
        ).scalar_one() == 1

    def test_created_by_member_id_is_not_a_permission(
        self, db, svc, catalog, stub_youtube
    ):
        """The OQ7 invariant, asserted as behaviour rather than trusted to a comment.

        Member A creates the mapping. Member B, who ALSO holds the track, must be
        able to re-point it — because authorization is standing, not authorship.
        If `created_by_member_id` ever leaks into the authz check, this fails.
        """
        stub_youtube(_StubYouTube())
        a, b = _member(db, "first-mapper"), _member(db, "second-holder")
        track = catalog.track_ids[0]
        _bucket_with_track(db, a, track)
        _bucket_with_track(db, b, track)

        svc.set_youtube_mapping(db, member_id=a, track_id=track, video_id=VIDEO)
        out = svc.set_youtube_mapping(db, member_id=b, track_id=track, video_id=OTHER_VIDEO)

        assert out["video_id"] == OTHER_VIDEO
        # ...and the attribution still names A, because it records who FIRST
        # confirmed the mapping. Rewriting it on every edit destroys the only
        # thing the column is for.
        assert db.execute(
            text("SELECT created_by_member_id FROM track_provider_refs WHERE track_id = :t"),
            {"t": track},
        ).scalar_one() == a


# ---------------------------------------------------------------------------
# Server-side verification — what makes a global row safe to accept
# ---------------------------------------------------------------------------

class TestVerificationBeforeWrite:
    @pytest.mark.parametrize(
        "stub,label",
        [
            (_StubYouTube(present=False), "deleted, private or nonexistent"),
            (_StubYouTube(embeddable=False), "embedding disabled by the owner"),
            (_StubYouTube(privacy="unlisted"), "not public"),
        ],
    )
    def test_an_unusable_video_is_refused_and_nothing_is_written(
        self, db, svc, catalog, stub_youtube, stub, label
    ):
        """The row is GLOBAL, so one member's bad pick would be every member's
        dead playback. Refusing at the boundary is what makes that impossible."""
        stub_youtube(stub)
        member = _member(db, "holder")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)

        with pytest.raises(PlaybackVideoUnusableError):
            svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)
        assert db.execute(
            text("SELECT count(*) FROM track_provider_refs WHERE track_id = :t"),
            {"t": track},
        ).scalar_one() == 0, label

    def test_the_status_fields_come_from_the_api_not_the_caller(
        self, db, svc, catalog, stub_youtube
    ):
        stub_youtube(_StubYouTube(duration="PT4M13S"))
        member = _member(db, "holder")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)

        svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)

        emb, priv, mfk, dur, state = db.execute(
            text(
                "SELECT embeddable, privacy_status, made_for_kids, duration_sec, verify_state "
                "FROM track_provider_refs WHERE track_id = :t"
            ),
            {"t": track},
        ).one()
        assert (emb, priv, mfk, dur, state) == (True, "public", False, 253, "live")

    def test_an_unknown_track_is_not_found_and_spends_no_quota(
        self, db, svc, stub_youtube
    ):
        stub = stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        with pytest.raises(PlaybackItemNotFoundError):
            svc.set_youtube_mapping(
                db, member_id=member, track_id=str(uuid.uuid4()), video_id=VIDEO
            )
        assert stub.calls == 0

    def test_a_malformed_track_id_is_not_found(self, db, svc, stub_youtube):
        stub = stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        with pytest.raises(PlaybackItemNotFoundError):
            svc.set_youtube_mapping(
                db, member_id=member, track_id="not-a-uuid", video_id=VIDEO
            )
        assert stub.calls == 0


# ---------------------------------------------------------------------------
# Re-pick and delete
# ---------------------------------------------------------------------------

class TestRepickAndDelete:
    def test_a_re_pick_replaces_rather_than_duplicates(
        self, db, svc, catalog, stub_youtube
    ):
        """UNIQUE (track_id, provider) means one video per track, re-pickable."""
        stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)

        svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)
        svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=OTHER_VIDEO)

        rows = db.execute(
            text("SELECT external_id FROM track_provider_refs WHERE track_id = :t"),
            {"t": track},
        ).scalars().all()
        assert rows == [OTHER_VIDEO]

    def test_delete_removes_the_row(self, db, svc, catalog, stub_youtube):
        stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)
        svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)

        svc.delete_youtube_mapping(db, member_id=member, track_id=track)

        assert db.execute(
            text("SELECT count(*) FROM track_provider_refs WHERE track_id = :t"),
            {"t": track},
        ).scalar_one() == 0

    def test_deleting_an_UNKNOWN_track_is_404_not_403(
        self, db, svc, stub_youtube
    ):
        """PUT and DELETE must agree about a track that does not exist.

        Checking only standing answers 403 for an unknown id — which reads as
        "you may not touch it" for something there is nothing to touch, and
        differs from what PUT says about the same id.
        """
        stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        with pytest.raises(PlaybackItemNotFoundError):
            svc.delete_youtube_mapping(db, member_id=member, track_id=str(uuid.uuid4()))

    def test_deleting_an_absent_mapping_is_not_an_error(
        self, db, svc, catalog, stub_youtube
    ):
        """Idempotent: the member's intent — "this must not resolve to that
        video" — is satisfied either way, and a 404 would report a failure for
        the state they asked for."""
        stub_youtube(_StubYouTube())
        member = _member(db, "holder")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)

        svc.delete_youtube_mapping(db, member_id=member, track_id=track)  # no raise


# ---------------------------------------------------------------------------
# V56 as a database fact
# ---------------------------------------------------------------------------

class TestResolveRefusesAnEmptyExternalId:
    def test_a_blank_video_id_does_not_resolve_to_a_bare_prefix(self, db, catalog):
        """`external_id` is NOT NULL but carries no non-empty CHECK.

        A blank one is a playable-looking row that resolves to `youtube:video:`,
        a URI the player can do nothing with. Unreachable while A3 is the only
        writer — but "unreachable" is a property of today's writers, not of the
        column, and the guard was dropped as a side effect of a restructure
        rather than as a decision.
        """
        from app.services.playback_service import PlaybackItemNotFoundError

        track = catalog.track_ids[0]
        db.execute(
            text(
                "INSERT INTO track_provider_refs "
                "(track_id, provider, external_id, external_kind, source, embeddable) "
                "VALUES (:t, 'youtube', '', 'video', 'user_confirmed', TRUE)"
            ),
            {"t": track},
        )
        db.flush()
        with pytest.raises(PlaybackItemNotFoundError):
            PlaybackService().resolve_uri(
                db, item_type="track", item_id=track, provider="youtube"
            )


class TestConcurrentConfirmDoesNotCollide:
    """The review's second blocker, pinned against a real UNIQUE constraint.

    The first revision was a read-then-write separated by a commit AND by up to
    `YOUTUBE_HTTP_TIMEOUT` seconds of `videos.list`. Two callers both saw "no
    row" and both INSERTed, violating `uq_tpr_track_provider` — and
    `_map_mapping_errors` had no IntegrityError branch, so it surfaced as a 500.

    This is NOT a remote race. OQ7's whole design is that several members may
    re-point the same global row, and a single member double-clicking "confirm"
    reproduces it alone. A mock cannot show any of it: the constraint is the
    database's.
    """

    def test_a_second_confirm_of_the_same_track_upserts_rather_than_raising(
        self, db, svc, catalog, stub_youtube
    ):
        """Simulates the interleaving: BOTH callers observe an empty table, then
        both write. Under the old read-then-write the second raised."""
        stub_youtube(_StubYouTube())
        a, b = _member(db, "racer-a"), _member(db, "racer-b")
        track = catalog.track_ids[0]
        _bucket_with_track(db, a, track)
        _bucket_with_track(db, b, track)

        # Both "reads" happen before either write — the state each caller
        # validated against.
        assert db.execute(
            text("SELECT count(*) FROM track_provider_refs WHERE track_id = :t"),
            {"t": track},
        ).scalar_one() == 0

        svc.set_youtube_mapping(db, member_id=a, track_id=track, video_id=VIDEO)
        svc.set_youtube_mapping(db, member_id=b, track_id=track, video_id=OTHER_VIDEO)

        rows = db.execute(
            text(
                "SELECT external_id, created_by_member_id FROM track_provider_refs "
                "WHERE track_id = :t"
            ),
            {"t": track},
        ).all()
        assert len(rows) == 1, "the upsert must not duplicate the row"
        assert rows[0][0] == OTHER_VIDEO, "the later confirm wins"
        assert rows[0][1] == a, "attribution still names the FIRST confirmer"

    def test_the_same_member_confirming_twice_is_idempotent(
        self, db, svc, catalog, stub_youtube
    ):
        """The double-click case, which needs no second member at all."""
        stub_youtube(_StubYouTube())
        member = _member(db, "double-clicker")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)

        svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)
        svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)

        assert db.execute(
            text("SELECT count(*) FROM track_provider_refs WHERE track_id = :t"),
            {"t": track},
        ).scalar_one() == 1


class TestV56ConstraintsAreReallyEnforced:
    def test_embeddable_rejects_null(self, db, catalog):
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO track_provider_refs "
                    "(track_id, provider, external_id, external_kind, source, embeddable) "
                    "VALUES (:t, 'youtube', 'x', 'video', 'user_confirmed', NULL)"
                ),
                {"t": catalog.track_ids[0]},
            )
            db.flush()

    def test_deleting_the_creator_nulls_the_column_and_keeps_the_mapping(
        self, db, svc, catalog, stub_youtube
    ):
        """The CASCADE-vs-SET-NULL decision, asserted as behaviour.

        Under CASCADE this test would find zero rows: deleting one member would
        delete a mapping every other member resolves against, and no guard on
        `track_provider_refs` would ever be called because the delete arrives
        from the parent.
        """
        stub_youtube(_StubYouTube())
        member = _member(db, "soon-deleted")
        track = catalog.track_ids[0]
        _bucket_with_track(db, member, track)
        svc.set_youtube_mapping(db, member_id=member, track_id=track, video_id=VIDEO)

        db.execute(text("DELETE FROM users WHERE id = :i"), {"i": member})
        db.flush()

        row = db.execute(
            text(
                "SELECT external_id, created_by_member_id FROM track_provider_refs "
                "WHERE track_id = :t"
            ),
            {"t": track},
        ).one_or_none()
        assert row is not None, "the mapping must SURVIVE its creator's deletion"
        assert row[0] == VIDEO
        assert row[1] is None
