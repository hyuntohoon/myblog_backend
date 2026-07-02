from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.api.schemas import LyricsResponse, LyricsSegment
from app.di import get_lyrics_service
from app.services.lyrics_service import LyricsService, LyricsTrackNotFoundError

_SPOTIFY_ID = "3n3Ppam7vgaVa1iaRUc9Lp"


def _override(app, svc):
    app.dependency_overrides[get_lyrics_service] = lambda: svc


def _override_db(app):
    from app.db.session import get_db
    app.dependency_overrides[get_db] = lambda: None


class TestLyricsRoute:
    def test_matched_track_returns_normalized_payload(self, client, app):
        svc = MagicMock()
        svc.get_normalized.return_value = LyricsResponse(
            availability="ok",
            source_kind="synced",
            trackable=True,
            segments=[LyricsSegment(i=0, text="hello", start_ms=1200)],
        )
        _override(app, svc)
        _override_db(app)

        resp = client.get(f"/api/lyrics/{_SPOTIFY_ID}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["availability"] == "ok"
        assert body["trackable"] is True
        assert body["segments"] == [{"i": 0, "text": "hello", "start_ms": 1200}]
        # keyed by the playback-side spotify id, resolved server-side (RFC decision)
        assert svc.get_normalized.call_args.kwargs["spotify_track_id"] == _SPOTIFY_ID
        app.dependency_overrides.clear()

    def test_empty_state_is_200_not_404(self, client, app):
        # A known track without viewable lyrics is a valid empty state the viewer renders.
        svc = MagicMock()
        svc.get_normalized.return_value = LyricsResponse(availability="unavailable")
        _override(app, svc)
        _override_db(app)

        resp = client.get(f"/api/lyrics/{_SPOTIFY_ID}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["availability"] == "unavailable"
        assert body["segments"] == []
        assert body["trackable"] is False
        app.dependency_overrides.clear()

    def test_unknown_spotify_id_maps_to_404(self, client, app):
        svc = MagicMock()
        svc.get_normalized.side_effect = LyricsTrackNotFoundError("nope")
        _override(app, svc)
        _override_db(app)

        resp = client.get("/api/lyrics/nope")

        assert resp.status_code == 404
        app.dependency_overrides.clear()

    def test_requires_jwt_in_prod(self, client, app):
        # Privacy gate: lyrics ride JWT only, never edge_guard alone. The dedicated
        # apigateway JWT route guards the edge; this pins the in-app belt-and-suspenders
        # (conftest forces ENV=local which bypasses auth — flip to prod, mirroring
        # test_playback.test_requires_jwt_in_prod).
        import app.core.auth as auth_module

        svc = MagicMock()
        _override(app, svc)
        _override_db(app)
        fake_settings = MagicMock()
        fake_settings.ENV = "prod"
        fake_settings.COGNITO_USER_POOL_ID = "ap-northeast-2_TestPool"
        fake_settings.COGNITO_REGION = "ap-northeast-2"

        with patch.object(auth_module, "settings", fake_settings):
            resp = client.get(f"/api/lyrics/{_SPOTIFY_ID}")

        assert resp.status_code == 401
        svc.get_normalized.assert_not_called()
        app.dependency_overrides.clear()


class TestLyricsPrivacyGate:
    def test_no_public_schema_references_lyrics(self, app):
        # Acceptance gate (FEAT-lyrics-viewer): lyric text and its presence stay out of
        # every other response type. The ONLY path allowed to reference a Lyrics* schema
        # is the JWT-gated /api/lyrics read; no other component schema may embed one or
        # carry a lyric-named property.
        spec = app.openapi()

        lyric_schemas = {n for n in spec["components"]["schemas"] if "lyric" in n.lower()}
        assert lyric_schemas == {"LyricsResponse", "LyricsSegment"}

        import json
        for path, ops in spec["paths"].items():
            if path.startswith("/api/lyrics/"):
                continue
            blob = json.dumps(ops)
            for name in lyric_schemas:
                assert f"schemas/{name}" not in blob, f"{path} references {name}"

        for name, schema in spec["components"]["schemas"].items():
            if name in lyric_schemas:
                continue
            blob = json.dumps(schema)
            assert "lyric" not in blob.lower(), f"component schema {name} mentions lyrics"
            for ln in lyric_schemas:
                assert f"schemas/{ln}" not in blob, f"component schema {name} embeds {ln}"


class TestGetNormalized:
    """LyricsService.get_normalized with a fake db — resolve + row-read wiring only
    (normalize_lyrics itself is covered in tests/test_lyrics_normalize.py)."""

    def _db(self, track_id, row):
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = track_id
        db.query.return_value.filter.return_value.one_or_none.return_value = row
        return db

    def test_unknown_spotify_id_raises(self):
        db = self._db(None, None)
        try:
            LyricsService().get_normalized(db, spotify_track_id="zzz")
            assert False, "expected LyricsTrackNotFoundError"
        except LyricsTrackNotFoundError:
            pass

    def test_known_track_missing_row_is_unavailable(self):
        db = self._db("11111111-1111-1111-1111-111111111111", None)
        out = LyricsService().get_normalized(db, spotify_track_id=_SPOTIFY_ID)
        assert out.availability == "unavailable"
        assert out.segments == []
