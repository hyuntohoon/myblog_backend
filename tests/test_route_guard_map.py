"""SEC-system-hardening Step 6: pin which endpoints are owner-gated.

This exists because of a hole found by adversarially reviewing Step 6, not in
the abstract. The refactor edited the guard *import line* in eleven route
modules, and the review mutation-tested that surface: rebinding `require_owner`
to `require_cognito_token` inside `publish.py`, `genres.py`, `research.py`,
`library.py` or `posts.py` silently downgrades those routes to "any valid pool
token" and **the entire unit suite still passed** — 0 failed in all five.
`tests/api/test_publish.py::TestPublishJwtRequired` only asserts 401 for a
*missing* token, which `require_cognito_token` alone also produces.

The Step 4 signed-JWT vectors do not cover this: they prove *authentication* is
correct, and what is at risk here is *authorization*. So the vectors could not
be the evidence for "no route lost a guard", and this file is.

With `FEAT-multi-user-accounts` 0c self-signup enabled, that downgrade means any
federated pool member can `POST /api/publish` or `DELETE /api/posts/{id}`.

Why endpoints and not URL paths
-------------------------------
FastAPI 0.141 changed `include_router`: child routes are no longer flattened
into `app.routes`, and the router-local `route.path` no longer carries the
prefix `main.py` mounted it under. This repo pins `fastapi>=0.110,<1.0`, so the
local venv resolved 0.136 while CI installed 0.141 — a path-keyed version of
this file passed locally and failed 28 assertions in CI. Endpoint identity
(`module:qualname`) is stable across both, so that is the key; the URL is kept
in a trailing comment for readability only.

How to change it
----------------
Adding an entry below is a deliberate act. If a diff here was not the point of
your change, you have changed a guard by accident — that is the purpose. A NEW
protected route also needs a matching entry in `infra/apigateway.tf`
(CLAUDE.md), so treat a failure here as the prompt to check both.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.main import app

_GUARD_NAMES = {"require_owner", "require_owner_or_draft_agent", "require_cognito_token"}

# Owner-only: editorial authoring/publish, genre taxonomy, and the owner's
# buckets/library/playback. A member token must not reach any of these.
OWNER_ONLY = [
    'app.api.routes.buckets:spotify_library_state',                  # GET /api/buckets/spotify-library/state
    'app.api.routes.buckets:spotify_library_sync',                   # POST /api/buckets/spotify-library/sync
    'app.api.routes.genres:create_genre',                            # POST /api/genres
    'app.api.routes.genres:update_genre',                            # PUT /api/genres/{genre_id}
    'app.api.routes.library:classify_saved_tracks',                  # POST /api/library/saved-tracks/classify
    'app.api.routes.library:fill_saved_track_genres',                # POST /api/library/saved-tracks/fill-genres
    'app.api.routes.library:refresh_recent',                         # POST /api/library/refresh-recent
    # SEC-member-listening-data-boundary Step 1 — the owner-global listening READS.
    # These are GETs, and they were edge_guard-only: any signed-in member's
    # dashboard rendered the owner's listening history off them. They have no
    # per-member scope to narrow to (no user column), so they are gated, not scoped.
    'app.api.routes.library:list_listened_albums',                   # GET /api/library/listened-albums
    'app.api.routes.library:list_recent_tracks',                     # GET /api/library/recent-tracks
    'app.api.routes.library:list_recently_listened',                 # GET /api/library/recently-listened
    'app.api.routes.library:list_saved_tracks',                      # GET /api/library/saved-tracks
    'app.api.routes.library:now_playing',                            # GET /api/library/now-playing
    'app.api.routes.library:play_events_artist_distribution',        # GET /api/library/play-events/artist-distribution
    'app.api.routes.library:play_events_genre_distribution',         # GET /api/library/play-events/genre-distribution
    'app.api.routes.library:saved_tracks_artist_distribution',       # GET /api/library/saved-tracks/artist-distribution
    'app.api.routes.library:saved_tracks_genre_distribution',        # GET /api/library/saved-tracks/genre-distribution
    'app.api.routes.lyrics:request_lyrics_translation',              # POST /api/lyrics/{spotify_track_id}/translation-request
    'app.api.routes.posts:delete_post',                              # DELETE /api/posts/{post_id}
    'app.api.routes.posts:get_post',                                 # GET /api/posts/{post_id}
    'app.api.routes.posts:list_posts',                               # GET /api/posts
    'app.api.routes.posts:restore_post',                             # PATCH /api/posts/{post_id}/restore
    'app.api.routes.posts:update_post',                              # PUT /api/posts/{post_id}
    'app.api.routes.publish:create_post',                            # POST /api/publish
    'app.api.routes.research:trigger_album_research',                # POST /api/research/albums/{album_id}
    'app.api.routes.reviews:owner_delete_review',                    # DELETE /api/reviews/{review_id}
    'app.api.routes.reviews:put_album_best_new',                     # PUT /api/reviews/albums/{album_id}/best-new
    'app.api.routes.todays_pick:add_to_pick_queue',                  # POST /api/todays-pick/queue
    'app.api.routes.todays_pick:delete_from_pick_queue',             # DELETE /api/todays-pick/queue/{queue_id}
    'app.api.routes.todays_pick:delete_todays_pick',                 # DELETE /api/todays-pick
    'app.api.routes.todays_pick:get_pick_queue',                     # GET /api/todays-pick/queue
    'app.api.routes.todays_pick:promote_from_pick_queue',            # POST /api/todays-pick/queue/{queue_id}/promote
    'app.api.routes.todays_pick:put_todays_pick',                    # PUT /api/todays-pick
    'app.api.routes.tracked_artists:import_spotify_followed_artists',# POST /api/me/tracked-artists/spotify-import
]

# The nightly draft agent may pass these, and only these. `create_post` still
# coerces a non-owner's post to status='draft', so passing is not permission to
# publish.
OWNER_OR_DRAFT_AGENT = [
    'app.api.routes.buckets:nightly_grow',# POST /api/buckets/nightly-grow
    'app.api.routes.posts:create_post',   # POST /api/posts
]


def _walk(routes) -> list[APIRoute]:
    """Every APIRoute reachable from `routes`, flat or nested.

    0.136 flattens included routers into `app.routes`; 0.141 keeps a wrapper
    node whose children hang off `original_router`. Recursing both shapes works
    on either, and `test_the_app_has_routes` below fails loudly rather than
    reporting an empty map if a future version does something else again.
    """
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        child = getattr(route, "routes", None)
        if isinstance(child, list):
            found.extend(_walk(child))
        inner = getattr(route, "original_router", None)
        if inner is not None and isinstance(getattr(inner, "routes", None), list):
            found.extend(_walk(inner.routes))
    return found


def _guards(route: APIRoute) -> set[str]:
    """Every auth dependency reachable from this route, by function name."""
    seen: set[str] = set()
    stack = [route.dependant]
    while stack:
        dep = stack.pop()
        fn = getattr(dep, "call", None)
        if fn is not None and getattr(fn, "__name__", "") in _GUARD_NAMES:
            seen.add(fn.__name__)
        stack.extend(dep.dependencies)
    return seen


def _live_map() -> dict[str, set[str]]:
    return {
        f"{r.endpoint.__module__}:{r.endpoint.__qualname__}": _guards(r)
        for r in _walk(app.routes)
    }


def test_the_app_has_routes() -> None:
    """Guard the guard: an empty map would make every assertion below vacuous.

    Not theoretical — it fired. The first version of this file walked
    `app.routes` flatly, which finds no APIRoute at all on FastAPI 0.141. CI
    failed here and in every pinned assertion instead of quietly passing on an
    empty map.
    """
    assert len(_live_map()) > 50


def test_guards_are_actually_detected() -> None:
    """Guard the guard, part two: prove `_guards` still sees dependencies.

    If a FastAPI change broke the `dependant` walk, every guard set would come
    back empty and the pins below would fail as 'ungated' — a confusing way to
    learn the harness broke. This says it directly.
    """
    live = _live_map()
    assert sum(1 for g in live.values() if g) > 20


@pytest.mark.parametrize("endpoint", OWNER_ONLY)
def test_owner_only_endpoint_is_owner_gated(endpoint: str) -> None:
    guards = _live_map().get(endpoint)
    assert guards is not None, f"{endpoint} no longer exists — update this list deliberately"
    assert "require_owner" in guards, (
        f"{endpoint} lost require_owner (now: {sorted(guards) or 'UNGATED'}). "
        f"With self-signup enabled this is reachable by any member of the pool."
    )


@pytest.mark.parametrize("endpoint", OWNER_OR_DRAFT_AGENT)
def test_draft_agent_endpoint_keeps_its_own_tier(endpoint: str) -> None:
    guards = _live_map().get(endpoint)
    assert guards is not None, f"{endpoint} no longer exists — update this list deliberately"
    assert "require_owner_or_draft_agent" in guards, (
        f"{endpoint} lost require_owner_or_draft_agent (now: {sorted(guards) or 'UNGATED'})"
    )


def test_no_owner_endpoint_was_downgraded_to_a_member_guard() -> None:
    """The precise failure the mutation reproduced.

    An owner endpoint whose only guard is `require_cognito_token` is the silent
    downgrade — it still 401s a missing token, so the existing 'JWT required'
    tests stay green while any member token gets through.
    """
    live = _live_map()
    downgraded = [e for e in OWNER_ONLY if live.get(e) == {"require_cognito_token"}]
    assert not downgraded, f"owner endpoints downgraded to a member guard: {downgraded}"


def test_no_owner_endpoint_became_ungated() -> None:
    live = _live_map()
    ungated = [e for e in OWNER_ONLY if not live.get(e)]
    assert not ungated, f"owner endpoints with no auth dependency at all: {ungated}"
