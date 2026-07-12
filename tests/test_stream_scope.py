"""FEAT-multi-user Phase 3 — library user-scope: live-tail owner gating.

`_unified_events` unions the member-owned import leg (spotify_stream_history,
strict user_id equality) with the OWNER's live poller cache
(spotify_track_play_events — no user_id column). The live leg may only ever be
included for the owner (or in local/dev, mirroring the auth-guard bypass); in
prod an unset OWNER_SUB fails CLOSED — nobody gets the live tail, so owner plays
can never leak into a member's analysis. DB-free: assert on the COMPILED SQL of
the returned selectable (which tables/filters it reads), not on rows.
"""
from __future__ import annotations

import uuid

import pytest

from app.services.library_service import LibraryService

OWNER_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")
MEMBER_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _compiled_sql(user_id: uuid.UUID) -> str:
    sq = LibraryService()._unified_events(user_id)
    return str(sq.element)


@pytest.fixture
def prod_settings(monkeypatch):
    """Flip the settings singleton to a prod-shaped env for the duration of a test.
    (conftest pins ENV=local; get_settings is lru_cached → clear around both edges.)"""
    import app.core.config as cfg

    def _set(owner_sub: str):
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("OWNER_SUB", owner_sub)
        cfg.get_settings.cache_clear()

    yield _set
    cfg.get_settings.cache_clear()


class TestLiveTailOwnerGate:
    def test_owner_gets_live_tail_in_prod(self, prod_settings):
        prod_settings(str(OWNER_UUID))
        sql = _compiled_sql(OWNER_UUID)
        assert "spotify_track_play_events" in sql
        assert "spotify_stream_history" in sql

    def test_member_gets_import_leg_only_in_prod(self, prod_settings):
        prod_settings(str(OWNER_UUID))
        sql = _compiled_sql(MEMBER_UUID)
        assert "spotify_track_play_events" not in sql
        assert "spotify_stream_history" in sql

    def test_unset_owner_sub_fails_closed(self, prod_settings):
        # Misconfigured prod (OWNER_SUB="") must NOT fall open — even a user who
        # would be the owner gets no live tail.
        prod_settings("")
        assert "spotify_track_play_events" not in _compiled_sql(OWNER_UUID)

    def test_local_env_keeps_live_tail(self):
        # conftest runs everything under ENV=local — the guard-bypass convention.
        assert "spotify_track_play_events" in _compiled_sql(MEMBER_UUID)

    def test_import_leg_is_user_scoped(self, prod_settings):
        # Both legs' SQL carries the strict user_id equality on the import table
        # (filter + the as_of horizon subquery).
        prod_settings(str(OWNER_UUID))
        for uid in (OWNER_UUID, MEMBER_UUID):
            assert "spotify_stream_history.user_id =" in _compiled_sql(uid)


class TestIncludeLiveTailHelper:
    def test_matrix(self, prod_settings):
        svc = LibraryService()
        prod_settings(str(OWNER_UUID))
        assert svc._include_live_tail(OWNER_UUID) is True
        assert svc._include_live_tail(MEMBER_UUID) is False
        prod_settings("")
        assert svc._include_live_tail(OWNER_UUID) is False
