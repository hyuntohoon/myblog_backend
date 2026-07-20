from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.api.routes.me import provisioned_member_id
from app.db.session import get_db
from app.di import get_bucket_service, get_tracked_artist_service
from app.services.bucket_service import BucketNotFoundError
from app.services.tracked_artist_service import (
    ArtistNotFoundError,
    TrackedArtistRateLimitError,
)

MEMBER_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
MEMBER_B = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
ARTIST_A = uuid.UUID("10000000-0000-0000-0000-000000000001")
ARTIST_B = uuid.UUID("10000000-0000-0000-0000-000000000002")
BUCKET_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
ADDED_AT = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)


def _artist(artist_id=ARTIST_A, name="Artist A", photo_url=None):
    return SimpleNamespace(id=artist_id, name=name, photo_url=photo_url)


def _override(app, bucket_svc, tracked_svc, member_id=MEMBER_A):
    app.dependency_overrides[get_db] = lambda: MagicMock(name="db")
    app.dependency_overrides[get_bucket_service] = lambda: bucket_svc
    app.dependency_overrides[get_tracked_artist_service] = lambda: tracked_svc
    app.dependency_overrides[provisioned_member_id] = lambda: member_id


class TestPreviewTrackedArtists:
    def test_expands_bucket_and_marks_existing_tracks(self, client, app):
        artists = [
            _artist(ARTIST_A, "Artist A", "https://cdn/a.jpg"),
            _artist(ARTIST_B, "Artist B"),
        ]
        bucket_svc = MagicMock()
        bucket_svc.bucket_catalog_artists.return_value = artists
        tracked_svc = MagicMock()
        tracked_svc.tracked_artist_ids.return_value = {ARTIST_B}
        _override(app, bucket_svc, tracked_svc)

        resp = client.post(
            "/api/me/tracked-artists/preview",
            json={"bucket_id": str(BUCKET_ID)},
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "candidates": [
                {
                    "artist_id": str(ARTIST_A),
                    "name": "Artist A",
                    "photo_url": "https://cdn/a.jpg",
                    "already_tracked": False,
                },
                {
                    "artist_id": str(ARTIST_B),
                    "name": "Artist B",
                    "photo_url": None,
                    "already_tracked": True,
                },
            ]
        }
        assert bucket_svc.bucket_catalog_artists.call_args.args[1:] == (
            MEMBER_A,
            BUCKET_ID,
        )
        assert tracked_svc.tracked_artist_ids.call_args.args[1] == MEMBER_A

    def test_other_members_bucket_is_404(self, client, app):
        bucket_svc = MagicMock()
        bucket_svc.bucket_catalog_artists.side_effect = BucketNotFoundError(BUCKET_ID)
        _override(app, bucket_svc, MagicMock(), member_id=MEMBER_B)

        resp = client.post(
            "/api/me/tracked-artists/preview",
            json={"bucket_id": str(BUCKET_ID)},
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Bucket not found"
        assert bucket_svc.bucket_catalog_artists.call_args.args[1] == MEMBER_B


class TestAddTrackedArtists:
    def test_bulk_add_returns_counts_and_member_scope(self, client, app):
        tracked_svc = MagicMock()
        tracked_svc.add_tracks.return_value = (1, 1)
        _override(app, MagicMock(), tracked_svc)

        resp = client.post(
            "/api/me/tracked-artists",
            json={"artist_ids": [str(ARTIST_B), str(ARTIST_A)]},
        )

        assert resp.status_code == 200
        assert resp.json() == {"added": 1, "already_tracked": 1}
        args = tracked_svc.add_tracks.call_args.args
        assert args[1] == MEMBER_A
        assert args[2] == [ARTIST_B, ARTIST_A]
        assert tracked_svc.add_tracks.call_args.kwargs["daily_cap"] == 500

    def test_unknown_artist_is_404(self, client, app):
        tracked_svc = MagicMock()
        tracked_svc.add_tracks.side_effect = ArtistNotFoundError(str(ARTIST_A))
        _override(app, MagicMock(), tracked_svc)

        resp = client.post(
            "/api/me/tracked-artists", json={"artist_ids": [str(ARTIST_A)]}
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Artist not found"

    def test_daily_cap_is_429(self, client, app):
        tracked_svc = MagicMock()
        tracked_svc.add_tracks.side_effect = TrackedArtistRateLimitError(
            "500+1/500 in 24h"
        )
        _override(app, MagicMock(), tracked_svc)

        resp = client.post(
            "/api/me/tracked-artists", json={"artist_ids": [str(ARTIST_A)]}
        )

        assert resp.status_code == 429
        assert resp.json()["detail"] == (
            "Daily tracked artist limit reached — try again later"
        )

    def test_malformed_artist_id_is_422(self, client, app):
        tracked_svc = MagicMock()
        _override(app, MagicMock(), tracked_svc)

        resp = client.post(
            "/api/me/tracked-artists", json={"artist_ids": ["not-a-uuid"]}
        )

        assert resp.status_code == 422
        tracked_svc.add_tracks.assert_not_called()


class TestTrackedArtistCrudIsolation:
    def test_member_b_cannot_see_or_delete_member_as_track(self, client, app):
        class StatefulTrackedService:
            def __init__(self):
                self.rows = {}

            def add_tracks(self, db, user_id, artist_ids, *, daily_cap=None):
                added = 0
                for artist_id in set(artist_ids):
                    key = (user_id, artist_id)
                    if key not in self.rows:
                        self.rows[key] = (
                            SimpleNamespace(artist_id=artist_id, added_at=ADDED_AT),
                            _artist(artist_id),
                        )
                        added += 1
                return added, len(set(artist_ids)) - added

            def list_tracks(self, db, user_id):
                return [
                    row for (owner, _), row in self.rows.items() if owner == user_id
                ]

            def delete_track(self, db, user_id, artist_id):
                return self.rows.pop((user_id, artist_id), None) is not None

        tracked_svc = StatefulTrackedService()
        bucket_svc = MagicMock()
        _override(app, bucket_svc, tracked_svc, member_id=MEMBER_A)
        added = client.post(
            "/api/me/tracked-artists", json={"artist_ids": [str(ARTIST_A)]}
        )
        assert added.status_code == 200

        app.dependency_overrides[provisioned_member_id] = lambda: MEMBER_B
        assert client.get("/api/me/tracked-artists").json() == []
        assert client.delete(f"/api/me/tracked-artists/{ARTIST_A}").status_code == 404

        app.dependency_overrides[provisioned_member_id] = lambda: MEMBER_A
        own_rows = client.get("/api/me/tracked-artists")
        assert own_rows.status_code == 200
        assert [row["artist_id"] for row in own_rows.json()] == [str(ARTIST_A)]
        assert client.delete(f"/api/me/tracked-artists/{ARTIST_A}").status_code == 204
        assert client.get("/api/me/tracked-artists").json() == []


# ── FEAT-for-you-releases Step 2: POST /spotify-import (owner-gated enqueue) ────


class TestSpotifyFollowImport:
    def _override_import(self, app, sqs, owner_id=MEMBER_A):
        from app.api.routes.me import provisioned_owner_id
        from app.di import get_sqs_client

        app.dependency_overrides[get_db] = lambda: MagicMock(name="db")
        app.dependency_overrides[get_sqs_client] = lambda: sqs
        app.dependency_overrides[provisioned_owner_id] = lambda: owner_id

    def test_enqueues_for_owner_and_returns_202(self, client, app):
        sqs = MagicMock()
        sqs.send_follow_import.return_value = True
        self._override_import(app, sqs)

        resp = client.post("/api/me/tracked-artists/spotify-import")

        assert resp.status_code == 202
        assert resp.json() == {"queued": True}
        sqs.send_follow_import.assert_called_once_with(str(MEMBER_A))

    def test_degrades_to_queued_false_without_queue(self, client, app):
        sqs = MagicMock()
        sqs.send_follow_import.return_value = False
        self._override_import(app, sqs)

        resp = client.post("/api/me/tracked-artists/spotify-import")

        assert resp.status_code == 202
        assert resp.json() == {"queued": False}

    def test_rejects_non_owner_in_prod(self, client, app):
        # The worker spends the OWNER's Spotify token — a signed-in non-owner
        # member must 403 before anything is enqueued (test_todays_pick pattern).
        from types import SimpleNamespace
        from unittest.mock import patch

        import app.core.auth as auth_module
        from app.core.auth import require_cognito_token
        from app.di import get_sqs_client

        sqs = MagicMock()
        app.dependency_overrides[get_db] = lambda: MagicMock(name="db")
        app.dependency_overrides[get_sqs_client] = lambda: sqs
        app.dependency_overrides[require_cognito_token] = lambda: {"sub": "some-member"}

        prod = SimpleNamespace(
            ENV="prod",
            COGNITO_USER_POOL_ID="ap-northeast-2_TestPool",
            COGNITO_REGION="ap-northeast-2",
            OWNER_SUB="owner-sub",
        )
        with patch.object(auth_module, "settings", prod):
            resp = client.post("/api/me/tracked-artists/spotify-import")

        assert resp.status_code == 403
        sqs.send_follow_import.assert_not_called()
        app.dependency_overrides.clear()
