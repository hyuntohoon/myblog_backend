from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from app.di import get_bucket_service
from app.services.bucket_service import (
    AlbumNotFoundError,
    ArtistNotFoundError,
    BucketNotFoundError,
    BucketTypeError,
    DuplicateItemError,
    ItemNotFoundError,
)


def _album(album_id="alb-1", title="Album", artists=("Artist A",)):
    a = MagicMock()
    a.id = album_id
    a.title = title
    a.cover_url = "https://cdn/cover.jpg"
    a.release_date = date(2026, 5, 1)
    a.popularity = 70
    a.artists = [MagicMock(name=n) for n in artists]
    # MagicMock(name=...) sets the mock's repr name, not a `.name` attr — set it.
    for m, n in zip(a.artists, artists):
        m.name = n
    return a


def _item(item_id="it-1", album_id="alb-1", position=0, status="candidate"):
    it = MagicMock()
    it.id = item_id
    it.album_id = album_id
    it.position = position
    it.note = None
    it.status = status
    it.post_id = None
    it.rec_reason = "신보"
    # research_selected is a validated bool on BucketItemResponse — a bare MagicMock
    # would fail validation, so set it explicitly (same reason as bucket.kind below).
    it.research_selected = False
    it.prep_tonight = False  # FEAT-editor-buckit: same validated-bool reason as above
    # FEAT-pocket-buckit Step 3: typed membership fields — a bare MagicMock would
    # auto-vivify truthy non-str values that fail validation, so set them explicitly.
    it.item_type = "album"
    it.track_id = None
    it.review_target_id = None
    it.artist_id = None  # FEAT-my-buckit-artist (V32): null on non-artist rows
    it.album = _album(album_id=album_id)
    return it


def _track(track_id="trk-1", title="어떤 트랙", album_id="alb-1", cover_url="https://cdn/track-cover.jpg"):
    """A Track ORM-shaped mock for the TrackBrief serializer. artists=[] so artist_names
    resolves to [] (a bare MagicMock .artists isn't iterable). `.album` is set explicitly
    (not left to MagicMock auto-vivification) since ARCH-global-playback-experience Step 3's
    cover_url reads track.album.cover_url — an unset `.album` would auto-vivify a MagicMock
    whose `.cover_url` is itself a MagicMock, failing TrackBrief's Optional[str] validation
    instead of exercising the real album/no-album path. Pass cover_url=None for a track with
    no resolvable album (the rare case _track_brief null-guards against)."""
    t = MagicMock()
    t.id = track_id
    t.title = title
    t.album_id = album_id
    t.duration_sec = 215
    t.artists = []
    t.album = None if cover_url is None else MagicMock(cover_url=cover_url)
    return t


def _nonalbum_item(item_id="it-trk", item_type="track", track_id="trk-1", position=0):
    """A non-album membership row as it will exist AFTER the STEP-2 relax (Step 6):
    album_id is NULL, no `.album`, a typed FK set. Step 3 only ships the read-side
    serializer that must tolerate this WITHOUT 500ing the board."""
    it = MagicMock()
    it.id = item_id
    it.item_type = item_type
    it.album_id = None
    it.track_id = track_id
    it.review_target_id = None
    it.artist_id = None  # FEAT-my-buckit-artist (V32): null unless an artist row
    it.position = position
    it.note = None
    it.status = "candidate"
    it.post_id = None
    it.rec_reason = None
    it.research_selected = False
    it.prep_tonight = False
    it.album = None
    # FEAT-pocket-buckit Step 6: a track/playback row carries a Track for the TrackBrief.
    it.track = _track(track_id=track_id)
    return it


def _artist(artist_id="art-1", name="아티스트 A", photo_url="https://cdn/a.jpg"):
    """An Artist ORM-shaped mock for the ArtistBrief serializer (FEAT-my-buckit-artist V32)."""
    a = MagicMock()
    a.id = artist_id
    a.name = name
    a.photo_url = photo_url
    return a


def _artist_item(item_id="it-art", artist_id="art-1", position=0):
    """An artist membership row (item_type='artist', album_id NULL, artist_id + .artist set)."""
    it = MagicMock()
    it.id = item_id
    it.item_type = "artist"
    it.album_id = None
    it.track_id = None
    it.review_target_id = None
    it.artist_id = artist_id
    it.position = position
    it.note = None
    it.status = "candidate"
    it.post_id = None
    it.rec_reason = None
    it.research_selected = False
    it.prep_tonight = False
    it.album = None
    it.track = None
    it.artist = _artist(artist_id=artist_id)
    return it


def _bucket(
    bucket_id="bk-1", name="꼭", position=0, is_done=False, items=(),
    children=(), kind="review", is_public=False, type="general",
):
    b = MagicMock()
    b.id = bucket_id
    b.name = name
    b.position = position
    b.color = None
    b.is_done = is_done
    # FEAT-public-bucket-multiuser Scope A: BucketResponse validates is_public as a
    # bool — set it explicitly so a bare MagicMock doesn't auto-vivify a non-bool.
    b.is_public = is_public
    # FEAT-spotify-library-sync: BucketResponse now carries `kind` (validated as a
    # str), so set it explicitly — a bare MagicMock auto-vivifies a non-string here.
    b.kind = kind
    # FEAT-my-buckit-artist (V32): BucketResponse carries `type` (validated str general|artist)
    # — set it explicitly so a bare MagicMock doesn't auto-vivify a non-string.
    b.type = type
    # research_mode is a validated str on BucketResponse (off|all|selected) — set it
    # explicitly so a bare MagicMock doesn't auto-vivify a non-string / truthy value.
    b.research_mode = "off"
    b.items = list(items)
    # list_buckets() attaches descendants on the transient `children_nodes`
    # attribute; the route serializes it recursively. MagicMock would otherwise
    # auto-vivify a truthy mock here, so set it explicitly.
    b.children_nodes = list(children)
    return b


def _override(app, svc, research_svc=None, genre_svc=None):
    # Import get_db lazily: a module-top import would pull app.core.config at
    # pytest collection time (before the autouse local_env fixture sets env),
    # caching an empty settings singleton that breaks content_sync in other
    # test modules.
    from app.db.session import get_db
    from app.di import get_genre_service, get_research_service

    app.dependency_overrides[get_bucket_service] = lambda: svc
    app.dependency_overrides[get_db] = lambda: MagicMock()
    # The bucket reads/mutations now batch research status (cover-badge seed). The
    # real ResearchService would run a query against the MagicMock db, so override
    # it; default returns an empty map → research_status=None on every item.
    if research_svc is None:
        research_svc = MagicMock()
        research_svc.status_map.return_value = {}
    app.dependency_overrides[get_research_service] = lambda: research_svc
    # FEAT-bucket-organize Step 2: bucket responses also batch genre labels. Same
    # reason — override so the real query doesn't hit the MagicMock db; default
    # returns an empty map → genres=[] on every album.
    if genre_svc is None:
        genre_svc = MagicMock()
        genre_svc.labels_map.return_value = {}
    app.dependency_overrides[get_genre_service] = lambda: genre_svc


class TestListBuckets:
    def test_list_returns_buckets_with_items(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item()])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["buckets"]) == 1
        bucket = data["buckets"][0]
        assert bucket["name"] == "꼭"
        assert bucket["items"][0]["album"]["title"] == "Album"
        assert bucket["items"][0]["already_reviewed"] is False
        assert bucket["items"][0]["rec_reason"] == "신보"
        # No genre map override → genres defaults to [] (FEAT-bucket-organize Step 2).
        assert bucket["items"][0]["album"]["genres"] == []
        app.dependency_overrides.clear()

    def test_mixed_album_and_nonalbum_bucket_serializes_without_500(self, client, app):
        # FEAT-pocket-buckit Step 3 — the load-bearing serializer-before-relax gate. A
        # non-album row (album_id NULL, no `.album`) must NOT 500 GET /api/buckets; the
        # pre-Step-3 `str(item.album_id)` / `_album_brief(item.album)` did exactly that.
        svc = MagicMock()
        svc.list_buckets.return_value = [
            _bucket(items=[_item(), _nonalbum_item()])
        ]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        items = resp.json()["buckets"][0]["items"]
        assert len(items) == 2
        album_item = next(i for i in items if i["item_type"] == "album")
        track_item = next(i for i in items if i["item_type"] == "track")
        assert album_item["album"]["title"] == "Album"
        assert album_item["album_id"] == "alb-1"
        # The non-album row carries no album payload and is not flagged reviewed.
        assert track_item["album"] is None
        assert track_item["album_id"] is None
        assert track_item["track_id"] == "trk-1"
        assert track_item["already_reviewed"] is False
        # reviewed/research/genre batch lookups must only have been asked about the album.
        assert svc.reviewed_album_ids.call_args[0][1] == ["alb-1"]
        app.dependency_overrides.clear()

    def test_list_surfaces_track_cover_url_for_playback_row(self, client, app):
        # ARCH-global-playback-experience Step 3: a playback-kind row's `.album` is
        # always null on the membership itself (queue rows key off track_id) — the
        # cover has to come from TrackBrief.cover_url, resolved server-side off the
        # track's own album, not from the (absent) item-level album payload.
        svc = MagicMock()
        svc.list_buckets.return_value = [
            _bucket(items=[_nonalbum_item(item_type="playback", track_id="trk-q1")])
        ]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        item = resp.json()["buckets"][0]["items"][0]
        assert item["album"] is None
        assert item["track"]["cover_url"] == "https://cdn/track-cover.jpg"
        app.dependency_overrides.clear()

    def test_list_surfaces_high_confidence_genres_on_album(self, client, app):
        # FEAT-bucket-organize Step 2: labels_map result flows onto AlbumBrief.genres,
        # primary ([0]) first.
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item(album_id="alb-g")])]
        svc.reviewed_album_ids.return_value = set()
        genre_svc = MagicMock()
        genre_svc.labels_map.return_value = {"alb-g": ["Rock", "Pop"]}
        _override(app, svc, genre_svc=genre_svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        album = resp.json()["buckets"][0]["items"][0]["album"]
        assert album["genres"] == ["Rock", "Pop"]
        app.dependency_overrides.clear()

    def test_already_reviewed_badge_set_from_post_albums(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item(album_id="alb-rev")])]
        svc.reviewed_album_ids.return_value = {"alb-rev"}
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        assert resp.json()["buckets"][0]["items"][0]["already_reviewed"] is True
        app.dependency_overrides.clear()

    def test_research_status_defaults_null_when_never_researched(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item()])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)  # default research_svc → empty status map

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        assert resp.json()["buckets"][0]["items"][0]["research_status"] is None
        app.dependency_overrides.clear()

    def test_research_status_seeded_from_status_map(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item(album_id="alb-done")])]
        svc.reviewed_album_ids.return_value = set()
        research_svc = MagicMock()
        research_svc.status_map.return_value = {"alb-done": "done"}
        _override(app, svc, research_svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        item = resp.json()["buckets"][0]["items"][0]
        # The cover badge can now paint the done dot on first paint (no per-cover GET).
        assert item["research_status"] == "done"
        research_svc.status_map.assert_called_once()
        app.dependency_overrides.clear()


    def test_list_includes_is_public(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(is_public=True)]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        assert resp.json()["buckets"][0]["is_public"] is True
        app.dependency_overrides.clear()

    def test_list_requires_jwt_in_prod(self, client):
        # FEAT-public-bucket-multiuser A5: the owner's full board (incl. private +
        # spotify_library buckets) must NOT be readable without a valid Cognito JWT.
        import app.core.auth as auth_module

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.get("/api/buckets")

        assert resp.status_code == 401


def _bucket_owner(handle="user-0468fd3c", display_name="지훈"):
    """The (bucket, owner) pairing list_public_buckets returns post-attribution:
    any member can publish a bucket, so the public read carries its owner."""
    u = MagicMock()
    u.handle = handle
    u.display_name = display_name
    return u


class TestPublicBuckets:
    def test_public_returns_whitelisted_shelves(self, client, app):
        svc = MagicMock()
        svc.list_public_buckets.return_value = [
            (_bucket(name="공개 셸프", is_public=True, items=[_item()]), _bucket_owner())
        ]
        svc.reviewed_album_ids.return_value = {"alb-1"}
        _override(app, svc)

        resp = client.get("/api/buckets/public")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["buckets"]) == 1
        bucket = data["buckets"][0]
        assert bucket["name"] == "공개 셸프"
        item = bucket["items"][0]
        assert item["album"]["title"] == "Album"
        assert item["already_reviewed"] is True
        # The service is what filters to is_public + kind='review'; the route calls it.
        assert svc.list_public_buckets.called
        app.dependency_overrides.clear()

    def test_public_buckets_carry_owner_attribution(self, client, app):
        # FEAT-multi-user-accounts P2: any member can publish a bucket, so the
        # public projection must attribute each shelf to its owner — a member's
        # bucket must never render as anonymous/owner-curated content. Only
        # already-public identity fields (handle/display_name) are exposed.
        svc = MagicMock()
        svc.list_public_buckets.return_value = [
            (
                _bucket(bucket_id="bk-m", name="멤버 셸프", is_public=True, items=[_item()]),
                _bucket_owner(handle="user-abcd1234", display_name="멤버"),
            )
        ]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets/public")

        assert resp.status_code == 200
        owner = resp.json()["buckets"][0]["owner"]
        assert owner == {"handle": "user-abcd1234", "display_name": "멤버"}
        app.dependency_overrides.clear()

    def test_public_skips_nonalbum_rows_without_500(self, client, app):
        # The UNAUTHENTICATED viewer is the higher-blast-radius path: list_public_buckets
        # dereferences it.album.id/.title/.artists, guarded only by `if it.album_id is not
        # None`. Pin both guards (the all_album_ids batch + the items projection) with a
        # mixed bucket so a future non-album row (post Step-6 relax) can't 500 the public viewer.
        svc = MagicMock()
        svc.list_public_buckets.return_value = [
            (_bucket(name="공개", is_public=True, items=[_item(), _nonalbum_item()]), _bucket_owner())
        ]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets/public")

        assert resp.status_code == 200
        items = resp.json()["buckets"][0]["items"]
        # Only the album row is projected; the non-album row is filtered out, not 500ing.
        assert len(items) == 1
        assert items[0]["album"]["title"] == "Album"
        app.dependency_overrides.clear()

    def test_public_omits_private_item_and_bucket_fields(self, client, app):
        # Security: the public projection must NOT echo private fields.
        svc = MagicMock()
        svc.list_public_buckets.return_value = [
            (_bucket(name="공개", is_public=True, items=[_item()]), _bucket_owner())
        ]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets/public")

        assert resp.status_code == 200
        bucket = resp.json()["buckets"][0]
        # bucket-level private/internal fields absent
        for forbidden in ("is_done", "kind", "research_mode", "is_public", "children"):
            assert forbidden not in bucket
        # item-level private fields absent (note / rec_reason / status / post_id / research)
        item = bucket["items"][0]
        for forbidden in ("note", "rec_reason", "status", "post_id", "research_selected", "research_status"):
            assert forbidden not in item
        app.dependency_overrides.clear()

    def test_public_does_not_require_jwt_in_prod(self, client, app):
        # Unlike GET /api/buckets, the public viewer endpoint must stay readable
        # without a token (only is_public buckets are exposed).
        import app.core.auth as auth_module

        svc = MagicMock()
        svc.list_public_buckets.return_value = []
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"
        with patch.object(auth_module, "settings", fake_settings):
            resp = client.get("/api/buckets/public")

        assert resp.status_code == 200
        app.dependency_overrides.clear()


class TestCreateBucket:
    def test_create_returns_201(self, client, app):
        svc = MagicMock()
        svc.create_bucket.return_value = _bucket(name="신보")
        _override(app, svc)

        resp = client.post("/api/buckets", json={"name": "신보"})

        assert resp.status_code == 201
        assert resp.json()["name"] == "신보"
        assert resp.json()["items"] == []
        # FEAT-my-buckit-artist (V32): default type is general.
        assert resp.json()["type"] == "general"
        app.dependency_overrides.clear()

    def test_create_artist_bucket_forwards_type(self, client, app):
        svc = MagicMock()
        svc.create_bucket.return_value = _bucket(name="좋아하는 아티스트", type="artist")
        _override(app, svc)

        resp = client.post(
            "/api/buckets", json={"name": "좋아하는 아티스트", "type": "artist"}
        )

        assert resp.status_code == 201
        assert resp.json()["type"] == "artist"
        assert svc.create_bucket.call_args.kwargs["type"] == "artist"
        app.dependency_overrides.clear()

    def test_daily_cap_maps_to_429(self, client, app):
        from app.services.bucket_service import BucketRateLimitError

        svc = MagicMock()
        svc.create_bucket.side_effect = BucketRateLimitError("30/30 in 24h")
        _override(app, svc)

        resp = client.post("/api/buckets", json={"name": "too many"})

        assert resp.status_code == 429
        assert resp.json()["detail"] == (
            "Daily bucket creation limit reached — try again later"
        )
        app.dependency_overrides.clear()

    def test_create_bad_type_rejected_by_schema_422(self, client, app):
        # The Literal on CreateBucketRequest rejects an out-of-enum type before the service.
        svc = MagicMock()
        _override(app, svc)
        resp = client.post("/api/buckets", json={"name": "x", "type": "bogus"})
        assert resp.status_code == 422
        svc.create_bucket.assert_not_called()
        app.dependency_overrides.clear()

    def test_blank_name_returns_400(self, client, app):
        # Whitespace passes pydantic min_length=1 but the service rejects it.
        svc = MagicMock()
        svc.create_bucket.side_effect = ValueError("name required")
        _override(app, svc)

        resp = client.post("/api/buckets", json={"name": " "})

        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_empty_name_rejected_by_schema_422(self, client, app):
        _override(app, MagicMock())
        resp = client.post("/api/buckets", json={"name": ""})
        assert resp.status_code == 422
        app.dependency_overrides.clear()

    def test_create_requires_jwt_in_prod(self, client):
        import app.core.auth as auth_module

        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.post("/api/buckets", json={"name": "x"})

        assert resp.status_code == 401


class TestUpdateBucket:
    def test_update_not_found_returns_404(self, client, app):
        svc = MagicMock()
        svc.update_bucket.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-x", json={"name": "new"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_second_done_bucket_returns_409(self, client, app):
        from sqlalchemy.exc import IntegrityError

        svc = MagicMock()
        svc.update_bucket.side_effect = IntegrityError("stmt", {}, Exception("dup"))
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1", json={"is_done": True})

        assert resp.status_code == 409
        app.dependency_overrides.clear()

    def test_update_forwards_only_set_fields(self, client, app):
        svc = MagicMock()
        svc.update_bucket.return_value = _bucket(name="renamed")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1", json={"name": "renamed"})

        assert resp.status_code == 200
        kwargs = svc.update_bucket.call_args.kwargs
        assert kwargs == {"name": "renamed"}
        app.dependency_overrides.clear()

    def test_update_color_null_forwards_explicit_none(self, client, app):
        # Resetting a bucket to the default ink sends {"color": null}. exclude_unset
        # must keep the field so color=None reaches the service as an explicit clear
        # (not stripped like an omitted field). Regression: the service ignored the
        # clear and the color could be set but never reset.
        svc = MagicMock()
        svc.update_bucket.return_value = _bucket(name="꼭")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1", json={"color": None})

        assert resp.status_code == 200
        kwargs = svc.update_bucket.call_args.kwargs
        assert kwargs == {"color": None}
        app.dependency_overrides.clear()


    def test_update_is_public_forwarded(self, client, app):
        svc = MagicMock()
        svc.update_bucket.return_value = _bucket(is_public=True)
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1", json={"is_public": True})

        assert resp.status_code == 200
        assert resp.json()["is_public"] is True
        assert svc.update_bucket.call_args.kwargs == {"is_public": True}
        app.dependency_overrides.clear()

    def test_make_spotify_library_public_returns_400(self, client, app):
        # The service guards against publishing the spotify_library bucket; the route
        # maps that ValueError to 400 (same path as a blank name).
        svc = MagicMock()
        svc.update_bucket.side_effect = ValueError(
            "the Spotify library bucket cannot be made public"
        )
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-lib", json={"is_public": True})

        assert resp.status_code == 400
        app.dependency_overrides.clear()


class TestDeleteBucket:
    def test_delete_returns_204(self, client, app):
        svc = MagicMock()
        svc.delete_bucket.return_value = True
        _override(app, svc)

        resp = client.delete("/api/buckets/bk-1")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_nonexistent_returns_404(self, client, app):
        svc = MagicMock()
        svc.delete_bucket.return_value = False
        _override(app, svc)

        resp = client.delete("/api/buckets/no-such")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestAddItem:
    def test_add_returns_201_with_item(self, client, app):
        svc = MagicMock()
        svc.add_item.return_value = _item()
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-1"})

        assert resp.status_code == 201
        body = resp.json()
        assert body["album_id"] == "alb-1"
        assert body["already_reviewed"] is False
        app.dependency_overrides.clear()

    def test_add_already_reviewed_album_sets_badge(self, client, app):
        svc = MagicMock()
        svc.add_item.return_value = _item(album_id="alb-rev")
        svc.reviewed_album_ids.return_value = {"alb-rev"}
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-rev"})

        assert resp.status_code == 201
        assert resp.json()["already_reviewed"] is True
        app.dependency_overrides.clear()

    def test_add_to_missing_bucket_returns_404(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-x/items", json={"album_id": "alb-1"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_daily_cap_maps_to_429(self, client, app):
        from app.services.bucket_service import BucketItemRateLimitError

        svc = MagicMock()
        svc.add_item.side_effect = BucketItemRateLimitError("500+1/500 in 24h")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-1"})

        assert resp.status_code == 429
        assert resp.json()["detail"] == (
            "Daily bucket item limit reached — try again later"
        )
        app.dependency_overrides.clear()

    def test_add_missing_album_returns_404(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = AlbumNotFoundError("alb-x")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-x"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_duplicate_album_in_bucket_returns_409(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = DuplicateItemError("alb-1")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"album_id": "alb-1"})

        assert resp.status_code == 409
        app.dependency_overrides.clear()

    def test_nonalbum_item_type_passed_through_to_service(self, client, app):
        # The route threads item_type + the typed target to the service. A track write needs a
        # track_id (the request validator rejects a track item without one).
        svc = MagicMock()
        svc.add_item.return_value = _nonalbum_item()
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        client.post(
            "/api/buckets/bk-1/items",
            json={"track_id": "trk-1", "item_type": "track"},
        )

        assert svc.add_item.call_args.kwargs["item_type"] == "track"
        assert svc.add_item.call_args.kwargs["track_id"] == "trk-1"
        app.dependency_overrides.clear()

    def test_track_write_returns_201_with_track_brief(self, client, app):
        # FEAT-pocket-buckit Step 6: non-album writes are enabled. A track add returns 201 with
        # a null album and a populated track brief (the rule-#4 serializer-before-relax gate is
        # satisfied — the serializer was prod-deployed first).
        svc = MagicMock()
        svc.add_item.return_value = _nonalbum_item()
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"track_id": "trk-1", "item_type": "track"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["item_type"] == "track"
        assert body["album_id"] is None
        assert body["track"]["id"] == "trk-1"
        # ARCH-global-playback-experience Step 3: resolved off track.album.cover_url,
        # not off the (null, for a track row) membership-level album.
        assert body["track"]["cover_url"] == "https://cdn/track-cover.jpg"
        app.dependency_overrides.clear()

    def test_track_write_cover_url_null_when_track_has_no_album(self, client, app):
        # Defensive null-safety: a track whose album can't be resolved must not 500 the
        # write — cover_url degrades to null, matching the front's existing placeholder.
        svc = MagicMock()
        svc.add_item.return_value = _nonalbum_item(track_id="trk-noalbum")
        svc.add_item.return_value.track = _track(track_id="trk-noalbum", cover_url=None)
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"track_id": "trk-noalbum", "item_type": "track"},
        )

        assert resp.status_code == 201
        assert resp.json()["track"]["cover_url"] is None
        app.dependency_overrides.clear()

    def test_track_write_without_track_id_rejected_by_schema(self, client, app):
        # The request validator requires the typed target for the named kind (422 before svc).
        svc = MagicMock()
        _override(app, svc)

        resp = client.post("/api/buckets/bk-1/items", json={"item_type": "track"})

        assert resp.status_code == 422
        svc.add_item.assert_not_called()
        app.dependency_overrides.clear()

    def test_missing_track_returns_404(self, client, app):
        from app.services.bucket_service import TrackNotFoundError

        svc = MagicMock()
        svc.add_item.side_effect = TrackNotFoundError("trk-x")
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"track_id": "trk-x", "item_type": "track"},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_unknown_item_type_rejected_by_schema(self, client, app):
        # The Literal on AddBucketItemRequest rejects an out-of-enum item_type (422)
        # before the service is even called.
        svc = MagicMock()
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"album_id": "alb-1", "item_type": "bogus"},
        )

        assert resp.status_code == 422
        svc.add_item.assert_not_called()
        app.dependency_overrides.clear()


class TestAddArtistItem:
    """FEAT-my-buckit-artist (V32): artist membership + type gate + source expansion."""

    def test_add_artist_returns_201_with_brief(self, client, app):
        svc = MagicMock()
        svc.add_item.return_value = _artist_item(artist_id="art-9")
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"item_type": "artist", "artist_id": "art-9"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["item_type"] == "artist"
        assert body["artist_id"] == "art-9"
        assert body["artist"]["id"] == "art-9"
        assert body["artist"]["name"] == "아티스트 A"
        # A direct artist add returns BucketItemResponse — no expansion shape.
        assert "expansion" not in body
        assert svc.add_item.call_args.kwargs["item_type"] == "artist"
        assert svc.add_item.call_args.kwargs["artist_id"] == "art-9"
        app.dependency_overrides.clear()

    def test_artist_add_without_target_rejected_by_schema_422(self, client, app):
        # Neither artist_id nor a source_* → the request validator rejects (422).
        svc = MagicMock()
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items", json={"item_type": "artist"}
        )

        assert resp.status_code == 422
        svc.add_item.assert_not_called()
        app.dependency_overrides.clear()

    def test_artist_add_with_two_targets_rejected_by_schema_422(self, client, app):
        # artist_id AND a source_* together → exactly-one validator rejects (422).
        svc = MagicMock()
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"item_type": "artist", "artist_id": "art-1", "source_album_id": "alb-1"},
        )

        assert resp.status_code == 422
        svc.add_item.assert_not_called()
        app.dependency_overrides.clear()

    def test_type_gate_album_into_artist_bucket_returns_400(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = BucketTypeError("artist only")
        _override(app, svc)

        resp = client.post("/api/buckets/bk-art/items", json={"album_id": "alb-1"})

        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_missing_artist_returns_404(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = ArtistNotFoundError("art-x")
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"item_type": "artist", "artist_id": "art-x"},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_duplicate_artist_returns_409(self, client, app):
        svc = MagicMock()
        svc.add_item.side_effect = DuplicateItemError("art-1")
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-1/items",
            json={"item_type": "artist", "artist_id": "art-1"},
        )

        assert resp.status_code == 409
        app.dependency_overrides.clear()

    def test_source_expansion_returns_201_with_added_skipped(self, client, app):
        # A source_album_id expands → expand_artist_source returns (added, skipped). The route
        # serializes the SUMMARY (no single row; id/position/status null), 201 since ≥1 added.
        svc = MagicMock()
        svc.expand_artist_source.return_value = (
            [_artist(artist_id="art-1", name="A"), _artist(artist_id="art-2", name="B")],
            [_artist(artist_id="art-3", name="C")],
        )
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-art/items",
            json={"item_type": "artist", "source_album_id": "alb-comp"},
        )

        assert resp.status_code == 201
        body = resp.json()
        # The expansion summary (ArtistExpansionResponse) carries no single-row id.
        assert "id" not in body
        assert body["item_type"] == "artist"
        assert [a["id"] for a in body["expansion"]["added"]] == ["art-1", "art-2"]
        assert [a["id"] for a in body["expansion"]["skipped"]] == ["art-3"]
        assert svc.expand_artist_source.call_args.kwargs["source_album_id"] == "alb-comp"
        # The single-row add path is NOT taken for a source_* add.
        svc.add_item.assert_not_called()
        app.dependency_overrides.clear()

    def test_source_expansion_noop_returns_200(self, client, app):
        # VA compilation → 0 added (or every credited artist already present) → 200, not 201.
        svc = MagicMock()
        svc.expand_artist_source.return_value = ([], [])
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-art/items",
            json={"item_type": "artist", "source_track_id": "trk-va"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["expansion"]["added"] == []
        assert svc.expand_artist_source.call_args.kwargs["source_track_id"] == "trk-va"
        app.dependency_overrides.clear()

    def test_source_expansion_daily_cap_maps_to_429(self, client, app):
        from app.services.bucket_service import BucketItemRateLimitError

        svc = MagicMock()
        svc.expand_artist_source.side_effect = BucketItemRateLimitError(
            "499+2/500 in 24h"
        )
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-art/items",
            json={"item_type": "artist", "source_album_id": "alb-comp"},
        )

        assert resp.status_code == 429
        assert resp.json()["detail"] == (
            "Daily bucket item limit reached — try again later"
        )
        app.dependency_overrides.clear()

    def test_expansion_missing_source_album_returns_404(self, client, app):
        svc = MagicMock()
        svc.expand_artist_source.side_effect = AlbumNotFoundError("alb-x")
        _override(app, svc)

        resp = client.post(
            "/api/buckets/bk-art/items",
            json={"item_type": "artist", "source_album_id": "alb-x"},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestUpdateItem:
    def test_update_status_returns_200(self, client, app):
        svc = MagicMock()
        svc.update_item.return_value = _item(status="published")
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.patch(
            "/api/buckets/bk-1/items/it-1", json={"status": "published"}
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "published"
        kwargs = svc.update_item.call_args.kwargs
        assert kwargs == {"status": "published"}
        app.dependency_overrides.clear()

    def test_update_nonalbum_item_returns_200_without_500(self, client, app):
        # FEAT-pocket-buckit Step 3: update_item has NO item_type gate, so post-Step-6 the
        # front edits a non-album row (note/status/prep_tonight) via this PATCH. An
        # unconditional str(item.album_id) would become uuid.UUID("None") in the UUID-typed
        # batch lookups → 500. Pin the null-guard with a non-album row.
        svc = MagicMock()
        nonalbum = _nonalbum_item()
        nonalbum.prep_tonight = True
        svc.update_item.return_value = nonalbum
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.patch(
            "/api/buckets/bk-1/items/it-trk", json={"prep_tonight": True}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["item_type"] == "track"
        assert body["album"] is None
        assert body["album_id"] is None
        # The album batch lookups were never asked about a "None" id.
        assert svc.reviewed_album_ids.call_args[0][1] == []
        app.dependency_overrides.clear()

    def test_update_prep_tonight_returns_200(self, client, app):
        # FEAT-editor-buckit Stage 1: the "오늘 밤 키우기" gate is a plain stored
        # bool — forwarded to the service and echoed on the response, no side-effect.
        svc = MagicMock()
        item = _item()
        item.prep_tonight = True
        svc.update_item.return_value = item
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.patch(
            "/api/buckets/bk-1/items/it-1", json={"prep_tonight": True}
        )

        assert resp.status_code == 200
        assert resp.json()["prep_tonight"] is True
        assert svc.update_item.call_args.kwargs == {"prep_tonight": True}
        app.dependency_overrides.clear()

    def test_update_missing_item_returns_404(self, client, app):
        svc = MagicMock()
        svc.update_item.side_effect = ItemNotFoundError("it-x")
        _override(app, svc)

        resp = client.patch("/api/buckets/bk-1/items/it-x", json={"note": "n"})

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_update_item_bad_status_returns_422(self, client, app):
        _override(app, MagicMock())
        resp = client.patch(
            "/api/buckets/bk-1/items/it-1", json={"status": "bogus"}
        )
        assert resp.status_code == 422
        app.dependency_overrides.clear()


class TestDeleteItem:
    def test_delete_returns_204(self, client, app):
        svc = MagicMock()
        svc.delete_item.return_value = True
        _override(app, svc)

        resp = client.delete("/api/buckets/bk-1/items/it-1")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_missing_returns_404(self, client, app):
        svc = MagicMock()
        svc.delete_item.return_value = False
        _override(app, svc)

        resp = client.delete("/api/buckets/bk-1/items/no-such")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestReorder:
    def test_reorder_returns_204(self, client, app):
        svc = MagicMock()
        _override(app, svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-1", "item_ids": ["it-2", "it-1"]}]},
        )

        assert resp.status_code == 204
        payload = svc.reorder.call_args.args[2]  # (db, member_id, payload)
        assert payload == [{"id": "bk-1", "item_ids": ["it-2", "it-1"]}]
        app.dependency_overrides.clear()

    def test_reorder_into_all_mode_bucket_enqueues_research(self, client, app):
        # An album dragged into an 'all'-mode bucket persists via /reorder (not
        # add_item), so the reorder path must enqueue the now-resident items.
        svc = MagicMock()
        all_bucket = _bucket(bucket_id="bk-all")
        all_bucket.research_mode = "all"
        svc.get_bucket.return_value = all_bucket
        research_svc = MagicMock()
        research_svc.enqueue_bucket.return_value = 1
        _override(app, svc, research_svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-all", "item_ids": ["it-1"]}]},
        )

        assert resp.status_code == 204
        research_svc.enqueue_bucket.assert_called_once()
        app.dependency_overrides.clear()

    def test_reorder_into_off_mode_bucket_skips_research(self, client, app):
        svc = MagicMock()
        svc.get_bucket.return_value = _bucket(bucket_id="bk-off")  # research_mode='off'
        research_svc = MagicMock()
        _override(app, svc, research_svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-off", "item_ids": ["it-1"]}]},
        )

        assert resp.status_code == 204
        research_svc.enqueue_bucket.assert_not_called()
        app.dependency_overrides.clear()

    def test_reorder_unknown_bucket_returns_404(self, client, app):
        svc = MagicMock()
        svc.reorder.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-x", "item_ids": []}]},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_reorder_unknown_item_returns_404(self, client, app):
        svc = MagicMock()
        svc.reorder.side_effect = ItemNotFoundError("it-x")
        _override(app, svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-1", "item_ids": ["it-x"]}]},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_reorder_non_artist_into_artist_bucket_returns_400(self, client, app):
        # FEAT-my-buckit-artist (V32): the move path's artist-only gate rejects a non-artist
        # item dragged into an Artist bucket (the hard invariant has no cross-table DB CHECK).
        svc = MagicMock()
        svc.reorder.side_effect = BucketTypeError("artist only")
        _override(app, svc)

        resp = client.put(
            "/api/buckets/reorder",
            json={"buckets": [{"id": "bk-art", "item_ids": ["it-album"]}]},
        )

        assert resp.status_code == 400
        app.dependency_overrides.clear()


class TestNestedTree:
    def test_child_nests_under_parent_not_at_top_level(self, client, app):
        # list_buckets() returns only roots; the child rides on the root's
        # children_nodes. The serialized response must place the child under
        # parent.children — never as a second top-level bucket.
        child = _bucket(bucket_id="bk-child", name="child", items=[_item()])
        root = _bucket(bucket_id="bk-root", name="root", children=[child])
        svc = MagicMock()
        svc.list_buckets.return_value = [root]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        data = resp.json()
        # Exactly one top-level bucket (the root).
        assert [b["id"] for b in data["buckets"]] == ["bk-root"]
        top = data["buckets"][0]
        assert [c["id"] for c in top["children"]] == ["bk-child"]
        # The child's item is reachable through the nested children, and the
        # already_reviewed batch covered tree items.
        assert top["children"][0]["items"][0]["album"]["title"] == "Album"
        app.dependency_overrides.clear()

    def test_root_with_no_children_serializes_empty_list(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(items=[_item()])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.get("/api/buckets")

        assert resp.status_code == 200
        assert resp.json()["buckets"][0]["children"] == []
        app.dependency_overrides.clear()


class TestMoveBucket:
    def test_move_reparent_success_returns_tree(self, client, app):
        svc = MagicMock()
        # move_bucket succeeds (return value unused by route); the route then
        # re-reads the nested tree via list_buckets.
        child = _bucket(bucket_id="bk-2", name="child")
        svc.list_buckets.return_value = [_bucket(bucket_id="bk-1", children=[child])]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-2/move", json={"parent_id": "bk-1", "position": 0}
        )

        assert resp.status_code == 200
        # Route forwarded parent_id + position to the service.
        kwargs = svc.move_bucket.call_args.kwargs  # also carries user_id (V40)
        assert kwargs["parent_id"] == "bk-1" and kwargs["position"] == 0
        # Returns the full nested tree.
        data = resp.json()
        assert [b["id"] for b in data["buckets"]] == ["bk-1"]
        assert [c["id"] for c in data["buckets"][0]["children"]] == ["bk-2"]
        app.dependency_overrides.clear()

    def test_move_to_root_parent_null(self, client, app):
        svc = MagicMock()
        svc.list_buckets.return_value = [_bucket(bucket_id="bk-2")]
        svc.reviewed_album_ids.return_value = set()
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-2/move", json={"parent_id": None, "position": 1}
        )

        assert resp.status_code == 200
        kwargs = svc.move_bucket.call_args.kwargs  # also carries user_id (V40)
        assert kwargs["parent_id"] is None and kwargs["position"] == 1
        app.dependency_overrides.clear()

    def test_move_cycle_returns_400(self, client, app):
        svc = MagicMock()
        svc.move_bucket.side_effect = ValueError(
            "cannot move a bucket under its own descendant"
        )
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-1/move", json={"parent_id": "bk-2", "position": 0}
        )

        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_move_missing_bucket_returns_404(self, client, app):
        svc = MagicMock()
        svc.move_bucket.side_effect = BucketNotFoundError("bk-x")
        _override(app, svc)

        resp = client.put(
            "/api/buckets/bk-x/move", json={"parent_id": None, "position": 0}
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_move_missing_position_returns_422(self, client, app):
        _override(app, MagicMock())
        resp = client.put("/api/buckets/bk-1/move", json={"parent_id": None})
        assert resp.status_code == 422
        app.dependency_overrides.clear()
