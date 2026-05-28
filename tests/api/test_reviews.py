from __future__ import annotations

from unittest.mock import MagicMock

from app.di import get_review_service
from app.services.review_service import InvalidTrackError, PostNotFoundError


POST_ID = "11111111-1111-1111-1111-111111111111"
TRACK_ID = "22222222-2222-2222-2222-222222222222"


def _track_review(track_id=TRACK_ID, rating=4.0, scale=5, notes=None):
    return {
        "track_id": track_id,
        "rating": rating,
        "scale": scale,
        "notes": notes,
    }


class TestGetBundle:
    def test_empty_bundle_returns_album_null_tracks_empty(self, client, app):
        mock_svc = MagicMock()
        mock_svc.get_bundle.return_value = {"album": None, "tracks": []}
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.get(f"/api/posts/{POST_ID}/reviews")

        assert resp.status_code == 200
        assert resp.json() == {"album": None, "tracks": []}
        app.dependency_overrides.clear()

    def test_bundle_returns_album_and_tracks(self, client, app):
        mock_svc = MagicMock()
        mock_svc.get_bundle.return_value = {
            "album": {"rating": 4.5, "scale": 5},
            "tracks": [_track_review(rating=3.5, notes="좋다")],
        }
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.get(f"/api/posts/{POST_ID}/reviews")

        assert resp.status_code == 200
        body = resp.json()
        assert body["album"] == {"rating": 4.5, "scale": 5}
        assert body["tracks"][0]["rating"] == 3.5
        assert body["tracks"][0]["notes"] == "좋다"
        app.dependency_overrides.clear()

    def test_get_post_not_found_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.get_bundle.side_effect = PostNotFoundError(POST_ID)
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.get(f"/api/posts/{POST_ID}/reviews")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestPutTrackReview:
    def test_put_returns_upserted_row(self, client, app):
        mock_svc = MagicMock()
        mock_svc.upsert_track_review.return_value = _track_review(rating=3.0)
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.put(
            f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}",
            json={"rating": 3.0, "scale": 5, "notes": None},
        )

        assert resp.status_code == 200
        assert resp.json()["rating"] == 3.0
        assert resp.json()["track_id"] == TRACK_ID
        app.dependency_overrides.clear()

    def test_put_idempotent_same_payload_same_response(self, client, app):
        mock_svc = MagicMock()
        mock_svc.upsert_track_review.return_value = _track_review(rating=4.0)
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        payload = {"rating": 4.0, "scale": 5}
        first = client.put(
            f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}", json=payload
        )
        second = client.put(
            f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}", json=payload
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
        assert mock_svc.upsert_track_review.call_count == 2
        app.dependency_overrides.clear()

    def test_put_rating_out_of_range_returns_422(self, client, app):
        resp = client.put(
            f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}",
            json={"rating": 10.0},
        )
        assert resp.status_code == 422

    def test_put_invalid_track_returns_400(self, client, app):
        mock_svc = MagicMock()
        mock_svc.upsert_track_review.side_effect = InvalidTrackError(TRACK_ID)
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.put(
            f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}",
            json={"rating": 3.0},
        )

        assert resp.status_code == 400
        assert TRACK_ID in resp.json()["detail"]
        app.dependency_overrides.clear()

    def test_put_post_not_found_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.upsert_track_review.side_effect = PostNotFoundError(POST_ID)
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.put(
            f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}",
            json={"rating": 3.0},
        )

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestDeleteTrackReview:
    def test_delete_existing_returns_204(self, client, app):
        mock_svc = MagicMock()
        mock_svc.delete_track_review.return_value = True
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.delete(f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}")

        assert resp.status_code == 204
        app.dependency_overrides.clear()

    def test_delete_missing_returns_404(self, client, app):
        mock_svc = MagicMock()
        mock_svc.delete_track_review.return_value = False
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.delete(f"/api/posts/{POST_ID}/reviews/tracks/{TRACK_ID}")

        assert resp.status_code == 404
        app.dependency_overrides.clear()


class TestBatchUpsert:
    def test_batch_returns_bundle_after_success(self, client, app):
        mock_svc = MagicMock()
        mock_svc.get_bundle.return_value = {
            "album": None,
            "tracks": [_track_review(rating=3.0), _track_review(track_id="t2", rating=4.5)],
        }
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.post(
            f"/api/posts/{POST_ID}/reviews/tracks/batch",
            json={
                "tracks": [
                    {"track_id": TRACK_ID, "rating": 3.0},
                    {"track_id": "t2", "rating": 4.5, "notes": "n"},
                ]
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["tracks"]) == 2
        # The route must call batch_upsert exactly once with all items intact.
        mock_svc.batch_upsert_track_reviews.assert_called_once()
        kwargs = mock_svc.batch_upsert_track_reviews.call_args.kwargs
        assert len(kwargs["items"]) == 2
        app.dependency_overrides.clear()

    def test_batch_with_invalid_track_returns_400_and_no_bundle_refetch(
        self, client, app
    ):
        """RFC: batch is all-or-nothing. An invalid track_id must abort the
        whole batch with 4xx — and the route must NOT call get_bundle afterward
        (which would imply partial-write success-path behavior)."""
        mock_svc = MagicMock()
        mock_svc.batch_upsert_track_reviews.side_effect = InvalidTrackError("t-bad")
        app.dependency_overrides[get_review_service] = lambda: mock_svc

        resp = client.post(
            f"/api/posts/{POST_ID}/reviews/tracks/batch",
            json={
                "tracks": [
                    {"track_id": TRACK_ID, "rating": 3.0},
                    {"track_id": "t-bad", "rating": 4.5},
                ]
            },
        )

        assert resp.status_code == 400
        assert "t-bad" in resp.json()["detail"]
        mock_svc.get_bundle.assert_not_called()
        app.dependency_overrides.clear()

    def test_batch_max_200(self, client, app):
        resp = client.post(
            f"/api/posts/{POST_ID}/reviews/tracks/batch",
            json={"tracks": [{"track_id": f"t{i}", "rating": 3.0} for i in range(201)]},
        )
        assert resp.status_code == 422


class TestBatchServiceAllOrNothing:
    """Service-level guard: pre-validate every track_id before writing.
    If any track is missing, the batch_upsert_track_reviews repo method is
    never called — proving no partial writes can leak even if the conflicting
    transaction would otherwise commit per-row."""

    def test_invalid_track_aborts_before_repo_write(self):
        from app.services.review_service import ReviewService

        post_repo = MagicMock()
        post_repo.get_by_id.return_value = object()  # post exists

        review_repo = MagicMock()
        review_repo.existing_track_ids.return_value = {TRACK_ID}  # second is missing

        svc = ReviewService(review_repo, post_repo)
        db = MagicMock()

        try:
            svc.batch_upsert_track_reviews(
                db,
                post_id=POST_ID,
                items=[
                    {"track_id": TRACK_ID, "rating": 3.0, "scale": 5, "notes": None},
                    {"track_id": "t-missing", "rating": 4.0, "scale": 5, "notes": None},
                ],
            )
            assert False, "expected InvalidTrackError"
        except InvalidTrackError as e:
            assert e.track_id == "t-missing"

        review_repo.batch_upsert_track_reviews.assert_not_called()
        db.commit.assert_not_called()
