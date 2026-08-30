"""SEC-member-listening-data-boundary Step 1 — the owner-global listening reads.

Nine `GET /api/library/*` routes read tables that have no user column
(`spotify_now_playing` is a CHECK-enforced singleton; `spotify_recent_albums`,
`spotify_recent_tracks`, `spotify_play_events` and `spotify_saved_tracks` have
no per-member source at all). They were `edge_guard`-only, and `SelfDashboard`
renders their widgets for *any* signed-in member — so a second member's
dashboard showed the OWNER's now-playing, recently-played albums, cumulative
listen counts and 좋아요 library.

Why this file exists alongside `tests/test_route_guard_map.py`
--------------------------------------------------------------
The guard map pins the *dependency*: it fails if `require_owner` is removed from
one of these endpoints. It cannot fail if `require_owner` is present but the
route still answers a member — and it says nothing about whether the handler ran.
These tests assert the observable outcome at the route: a valid non-owner token
gets **403 and the service is never called**, so the data never leaves the DB
layer even in a response the caller discards.

The whole suite runs with `ENV=local` (see `tests/conftest.py`), where
`require_owner` deliberately bypasses. Every test here therefore patches
`app.core.auth.settings` to a prod stub — the `tests/api/test_todays_pick.py`
pattern. Without that patch these tests would pass against a completely
ungated route, which is exactly the false green this file is meant to prevent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.di import get_library_service

# Every owner-global listening read closed by Step 1, with the service method it
# must not reach. Parametrized rather than written out so a tenth route added to
# the group without a guard is one line away from being covered.
OWNER_ONLY_READS = [
    ("/api/library/now-playing", "get_now_playing"),
    ("/api/library/recently-listened", "list_recently_listened"),
    ("/api/library/recent-tracks", "list_recent_tracks"),
    ("/api/library/listened-albums", "list_listened_albums"),
    ("/api/library/saved-tracks", "list_saved_tracks"),
    ("/api/library/saved-tracks/genre-distribution", "saved_tracks_genre_distribution"),
    ("/api/library/saved-tracks/artist-distribution", "saved_tracks_artist_distribution"),
    ("/api/library/play-events/genre-distribution", "play_events_genre_distribution"),
    ("/api/library/play-events/artist-distribution", "play_events_artist_distribution"),
]

_PATHS = [p for p, _ in OWNER_ONLY_READS]


def _prod_settings(owner_sub: str) -> SimpleNamespace:
    """A prod settings stub that forces `require_owner` past its local/dev bypass."""
    return SimpleNamespace(
        ENV="prod",
        COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
        COGNITO_REGION="ap-northeast-2",
        OWNER_SUB=owner_sub,
    )


def _override(app, svc):
    from app.db.session import get_db

    app.dependency_overrides[get_library_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()


@pytest.fixture(autouse=True)
def _no_override_leak(app):
    """Clear dependency overrides even when an assertion fails.

    These tests install a fake `require_cognito_token` and a MagicMock `get_db`.
    Clearing at the end of the test body (the pattern this file first used) leaks
    both on any failure, turning one real failure into a cascade of unrelated ones
    in every later test in the session. Review caught it.
    """
    yield
    app.dependency_overrides.clear()


def _as(app, sub: str):
    """Answer the token dependency with these claims, bypassing signature checks.

    `require_owner` depends on `require_cognito_token`; overriding the latter
    exercises the *authorization* tier in isolation from authentication, which
    `tests/test_cognito_token_vectors.py` already covers.
    """
    from app.core.auth import require_cognito_token

    app.dependency_overrides[require_cognito_token] = lambda: {"sub": sub}


class TestNonOwnerIsRefused:
    @pytest.mark.parametrize(("path", "method_name"), OWNER_ONLY_READS)
    def test_member_token_gets_403_and_no_read_happens(self, client, app, path, method_name):
        import app.core.auth as auth_module

        svc = MagicMock()
        _override(app, svc)
        _as(app, "some-other-member")

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(path)

        assert resp.status_code == 403, f"{path} answered a non-owner"
        # The point of the boundary: the owner's rows are never even loaded.
        assert getattr(svc, method_name).call_count == 0

    @pytest.mark.parametrize("path", _PATHS)
    def test_missing_owner_sub_in_prod_is_503_not_open(self, client, app, path):
        """A misconfigured deploy must fail closed, never fall back to serving."""
        import app.core.auth as auth_module

        svc = MagicMock()
        _override(app, svc)
        _as(app, "anyone")

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="")):
            resp = client.get(path)

        assert resp.status_code == 503, f"{path} did not fail closed on unset OWNER_SUB"


class TestOwnerStillReads:
    """The gate must not cost the owner anything — the regression that matters most."""

    @pytest.mark.parametrize("path", _PATHS)
    def test_owner_token_passes_the_gate(self, client, app, path):
        import app.core.auth as auth_module

        svc = MagicMock()
        # Distributions/lists are shaped by the response models; MagicMock returns
        # would not serialize, so give each read an empty-but-valid result.
        svc.get_now_playing.return_value = None
        svc.list_recently_listened.return_value = []
        svc.last_recent_synced_at.return_value = None
        svc.list_recent_tracks.return_value = []
        svc.last_recent_tracks_synced_at.return_value = None
        svc.list_listened_albums.return_value = ([], 0)
        svc.list_saved_tracks.return_value = ([], 0, None)
        # The distribution services return a plain dict, not an ORM row.
        for d in (
            "saved_tracks_genre_distribution",
            "saved_tracks_artist_distribution",
            "play_events_genre_distribution",
            "play_events_artist_distribution",
        ):
            getattr(svc, d).return_value = {"items": [], "unclassified_count": 0, "total": 0}
        _override(app, svc)
        _as(app, "owner-sub")

        with patch.object(auth_module, "settings", _prod_settings(owner_sub="owner-sub")):
            resp = client.get(path)

        assert resp.status_code == 200, f"{path} refused the owner: {resp.text[:200]}"
