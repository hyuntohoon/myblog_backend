"""Disconnect revokes lyrics discovery scopes atomically (FEAT-lyrics-listening-
experience Step 4, OQ6).

The unit tests around `IntegrationService.disconnect` see a mocked Session and are
therefore blind to the only thing that matters here: that the credential delete and the
scope revoke land in ONE transaction. Between a committed delete and a separate revoke
there is a window in which a member has withdrawn their connection while their saved
albums and plays keep producing translation demand — and a mock cannot tell a single
transaction from two ([[feedback-sa-session-lifecycle-mock-blind]]).

Deliberately does NOT create the V57 tables it exercises. A `CREATE TABLE IF NOT EXISTS`
here would make this suite green against a test branch where the migration never landed,
which is precisely the failure it is supposed to catch
([[feedback-integration-test-must-not-create-its-own-schema]]) — so it asserts their
existence and fails loudly instead.

Gated on TEST_DB_URL (Neon test branch).
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from myblog_shared_db.lyrics_demand import LyricsDemandStore

from app.services.integration_service import (
    LASTFM_PROVIDER,
    SPOTIFY_DISCOVERY_ORIGINS,
    SPOTIFY_PROVIDER,
    IntegrationService,
)

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DB_URL not set (Neon test branch)"),
]

_PREFIX = "disc_demand_"
_HANDLE = "disc-demand-"


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    missing = []
    with eng.begin() as conn:
        for table in ("lyrics_discovery_scopes", "lyrics_album_demands",
                      "lyrics_album_jobs", "user_integrations"):
            if not conn.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
            ).scalar():
                missing.append(table)
    if missing:
        pytest.fail(
            "test branch is missing %s — apply the migration to the test database "
            "rather than letting this suite create it (see "
            "reference-neon-test-branch-migration-drift)" % ", ".join(missing)
        )
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, future=True)


def _cleanup(factory):
    with factory() as s, s.begin():
        s.execute(text("DELETE FROM lyrics_album_jobs WHERE spotify_album_id LIKE :p"),
                  {"p": f"{_PREFIX}%"})
        s.execute(text("DELETE FROM users WHERE handle LIKE :p"), {"p": f"{_HANDLE}%"})


@pytest.fixture
def member(factory):
    _cleanup(factory)
    uid = uuid.uuid4()
    with factory() as s, s.begin():
        s.execute(
            text("INSERT INTO users (id, handle, display_name) VALUES (:i, :h, 'D')"),
            {"i": str(uid), "h": f"{_HANDLE}{uid.hex[:8]}"},
        )
        s.execute(
            # The payload goes through a bind parameter: a JSON literal inside text()
            # would have its ":1" read as a bind parameter name.
            text("INSERT INTO user_integrations (user_id, provider, payload, status) "
                 "VALUES (:u, 'spotify', :p, 'connected')"),
            {"u": str(uid), "p": '{"v": 1}'},
        )
    # Give the member live demand on both origins.
    with factory() as s:
        store = LyricsDemandStore(s.connection())
        for origin in SPOTIFY_DISCOVERY_ORIGINS:
            scope = store.reset_scope(uid, origin)
            store.add_demand(uid, scope["id"], scope["generation"],
                             f"{_PREFIX}{origin}_{uid.hex[:6]}",
                             f"{_PREFIX}{origin}_{uid.hex[:6]}")
        s.commit()
    yield uid
    _cleanup(factory)


def _state(factory, uid):
    with factory() as s:
        demands = s.execute(
            text("SELECT count(*) FROM lyrics_album_demands d "
                 "JOIN lyrics_discovery_scopes sc ON sc.id = d.scope_id "
                 "WHERE sc.user_id = :u"), {"u": str(uid)}).scalar()
        actives = s.execute(
            text("SELECT count(*) FROM lyrics_discovery_scopes "
                 "WHERE user_id = :u AND active"), {"u": str(uid)}).scalar()
        creds = s.execute(
            text("SELECT count(*) FROM user_integrations "
                 "WHERE user_id = :u AND provider = 'spotify'"), {"u": str(uid)}).scalar()
    return {"demands": demands, "active_scopes": actives, "connections": creds}


# The fixture seeds exactly one demand per origin by looping SPOTIFY_DISCOVERY_ORIGINS,
# so the expected count IS the length of that list — deriving it here is not the same
# as the worker's revoke test, where a literal was right because Step 5's `follow` scope
# only opens when there is a follow observation. Step 5 made this three; the assertion
# that matters is that disconnect takes it to zero whatever the list holds.
_SEEDED = len(SPOTIFY_DISCOVERY_ORIGINS)


def test_disconnect_revokes_every_spotify_origin(factory, member):
    before = _state(factory, member)
    assert before == {"demands": _SEEDED, "active_scopes": _SEEDED, "connections": 1}

    with factory() as db:
        assert IntegrationService().disconnect(db, member, SPOTIFY_PROVIDER) is True

    assert _state(factory, member) == {
        "demands": 0, "active_scopes": 0, "connections": 0}


def test_the_revoke_and_the_delete_are_one_transaction(factory, member, monkeypatch):
    """Break the revoke and the connection must survive.

    If these were two transactions the delete would already be committed and the
    member would be left disconnected but still producing demand — the exact window
    OQ6 requires to not exist.
    """
    import app.services.integration_service as mod

    class _Boom(LyricsDemandStore):
        def revoke_scopes(self, *a, **kw):
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(mod, "LyricsDemandStore", _Boom)

    with factory() as db:
        with pytest.raises(RuntimeError):
            IntegrationService().disconnect(db, member, SPOTIFY_PROVIDER)

    # Nothing moved: the whole unit rolled back.
    assert _state(factory, member) == {
        "demands": _SEEDED, "active_scopes": _SEEDED, "connections": 1}


def test_disconnecting_lastfm_leaves_spotify_demand_alone(factory, member):
    with factory() as db:
        # No Last.fm row exists, so this is the idempotent no-op path...
        assert IntegrationService().disconnect(db, member, LASTFM_PROVIDER) is False
    assert _state(factory, member)["demands"] == _SEEDED

    with factory() as db, db.begin():
        db.execute(text("INSERT INTO user_integrations (user_id, provider, username, "
                        "status) VALUES (:u, 'lastfm', 'rj', 'connected')"),
                   {"u": str(member)})
    with factory() as db:
        assert IntegrationService().disconnect(db, member, LASTFM_PROVIDER) is True
    # ...and a real Last.fm disconnect must not touch Spotify-derived demand either.
    assert _state(factory, member) == {
        "demands": _SEEDED, "active_scopes": _SEEDED, "connections": 1}


def test_a_disconnect_cannot_reach_another_members_demand(factory, member):
    """The store scopes every revoke to the acting member; a second member's demand
    for the very same album must survive."""
    other = uuid.uuid4()
    shared = f"{_PREFIX}shared_{other.hex[:6]}"
    with factory() as s, s.begin():
        s.execute(text("INSERT INTO users (id, handle, display_name) "
                       "VALUES (:i, :h, 'O')"),
                  {"i": str(other), "h": f"{_HANDLE}other-{other.hex[:8]}"})
    with factory() as s:
        store = LyricsDemandStore(s.connection())
        for uid in (member, other):
            scope = store.reset_scope(uid, "saved")
            store.add_demand(uid, scope["id"], scope["generation"], shared, shared)
        s.commit()

    with factory() as db:
        IntegrationService().disconnect(db, member, SPOTIFY_PROVIDER)

    assert _state(factory, member)["demands"] == 0
    with factory() as s:
        others = s.execute(
            text("SELECT origin_key FROM lyrics_album_demands d "
                 "JOIN lyrics_discovery_scopes sc ON sc.id = d.scope_id "
                 "WHERE sc.user_id = :u"), {"u": str(other)}).scalars().all()
        # The shared album's job stays live because the other member still wants it.
        cancelled = s.execute(
            text("SELECT cancelled FROM lyrics_album_jobs WHERE spotify_album_id = :s"),
            {"s": shared}).scalar()
    assert others == [shared]
    assert cancelled is False


def test_disconnect_removes_the_spotify_follow_mirror_and_keeps_manual_tracking(
        factory, member):
    """OQ6's second half: the demand AND the member/artist provenance derived from it.

    Step 5 writes a row-for-row copy of whom the member follows on Spotify into their
    site tracking. A disconnected member is no longer polled, so the reconciler that
    prunes those edges can never run again — a copy left here is permanent, not stale.
    """
    with factory() as s, s.begin():
        mirrored = uuid.uuid4()
        also_manual = uuid.uuid4()
        for artist_id in (mirrored, also_manual):
            s.execute(text("INSERT INTO artists (id, name, spotify_id) "
                           "VALUES (:i, 'Disc test', :sp)"),
                      {"i": str(artist_id), "sp": f"{_PREFIX}{artist_id.hex[:12]}"})
            s.execute(text("INSERT INTO user_artist_tracks (user_id, artist_id) "
                           "VALUES (:u, :a)"), {"u": str(member), "a": str(artist_id)})
            s.execute(text("INSERT INTO user_artist_track_origins (user_id, artist_id, "
                           "origin) VALUES (:u, :a, 'spotify_follow')"),
                      {"u": str(member), "a": str(artist_id)})
        # This one the member also added by hand — OQ2's union must hold it up.
        s.execute(text("INSERT INTO user_artist_track_origins (user_id, artist_id, "
                       "origin) VALUES (:u, :a, 'manual')"),
                  {"u": str(member), "a": str(also_manual)})

    with factory() as db:
        assert IntegrationService().disconnect(db, member, SPOTIFY_PROVIDER) is True

    with factory() as s:
        edges = set(s.execute(
            text("SELECT artist_id FROM user_artist_tracks WHERE user_id = :u"),
            {"u": str(member)}).scalars())
        origins = set(s.execute(
            text("SELECT artist_id, origin FROM user_artist_track_origins "
                 "WHERE user_id = :u"), {"u": str(member)}).all())
    assert edges == {also_manual}, "the pure Spotify mirror goes, the manual edge stays"
    assert origins == {(also_manual, "manual")}

    with factory() as s, s.begin():
        s.execute(text("DELETE FROM user_artist_tracks WHERE user_id = :u"),
                  {"u": str(member)})
        s.execute(text("DELETE FROM artists WHERE spotify_id LIKE :p"),
                  {"p": f"{_PREFIX}%"})
