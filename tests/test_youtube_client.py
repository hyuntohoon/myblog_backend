"""FEAT-youtube-playback-provider A3 — the backend YouTube client.

WHY THIS FILE EXISTS. The client shipped with zero tests while its music twin
had ~25, including the ones the A2 key leak produced. The CODE was right — I
traced every channel — but "the code is right" is what was also true of the
music client one revision before the leak was found. The A2 decisions log's own
rule is that when you close a leak you enumerate the channels BEFORE writing the
test that says it is closed; satisfying that in one copy of three is not
satisfying it.

Twin of the credential and payload-hardening tests in
`myblog_music/tests/test_youtube_candidates.py`. A fix to either belongs in
both, in the same change (CLAUDE.md, cross-repo duplicated code).
"""
from __future__ import annotations

import pytest

from app.clients.youtube_client import (
    DAILY_QUOTA_REASONS,
    RATE_LIMIT_REASONS,
    VIDEOS_LIST_MAX_IDS,
    YouTubeClient,
    YouTubeError,
    YouTubeNotConfigured,
    YouTubeQuotaExhausted,
    YouTubeRateLimited,
)


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


# --------------------------------------------------------------------------
# Credential handling — the channel the A2 review found open
# --------------------------------------------------------------------------

def test_api_key_travels_in_a_header_never_in_the_query_string(monkeypatch):
    """httpx logs `request.url` at INFO on every completed request.

    A `?key=` credential is therefore one `basicConfig(level=INFO)` — or one
    Lambda ApplicationLogLevel change — away from CloudWatch. The prod Lambda's
    root logger defaulting to WARNING is an unset default, not a control.
    """
    seen = {}

    def capture(url, params=None, headers=None, timeout=None, **k):
        seen.update(url=url, params=params or {}, headers=headers or {})
        return _FakeResponse(200, {"items": []})

    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "SUPERSECRETKEY", raising=False)
    monkeypatch.setattr("httpx.get", capture)
    YouTubeClient().list_videos(["abc"])

    assert seen["headers"].get("X-goog-api-key") == "SUPERSECRETKEY"
    assert "key" not in seen["params"]
    assert "SUPERSECRETKEY" not in str(seen["params"])
    assert "SUPERSECRETKEY" not in seen["url"]


def test_transport_failure_does_not_leak_the_url_or_key(monkeypatch):
    import httpx
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "SUPERSECRETKEY", raising=False)

    def boom(*a, **k):
        raise httpx.ConnectTimeout("timed out for https://...&key=SUPERSECRETKEY")

    monkeypatch.setattr("httpx.get", boom)
    with pytest.raises(YouTubeError) as ei:
        YouTubeClient().list_videos(["abc"])
    assert "SUPERSECRETKEY" not in str(ei.value)
    assert ei.value.__cause__ is None, "`raise ... from None` keeps the leaky cause off the traceback"


def test_missing_api_key_fails_closed(monkeypatch):
    """No key must mean REFUSE, never "write the mapping unverified".

    The row is global: an unverified write is one member poisoning everyone's
    playback.
    """
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "", raising=False)
    with pytest.raises(YouTubeNotConfigured):
        YouTubeClient().list_videos(["abc"])


def test_an_unconfigured_client_makes_no_request_at_all(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: called.update(n=called["n"] + 1))
    with pytest.raises(YouTubeNotConfigured):
        YouTubeClient().list_videos(["abc"])
    assert called["n"] == 0


# --------------------------------------------------------------------------
# Error taxonomy — must match the music twin
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(DAILY_QUOTA_REASONS))
def test_daily_quota_reasons(monkeypatch, reason):
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(
        403, {"error": {"errors": [{"reason": reason}]}}))
    with pytest.raises(YouTubeQuotaExhausted):
        YouTubeClient().list_videos(["abc"])


@pytest.mark.parametrize("reason", sorted(RATE_LIMIT_REASONS))
def test_rate_limit_reasons_are_not_the_daily_quota(monkeypatch, reason):
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(
        403, {"error": {"errors": [{"reason": reason}]}}))
    with pytest.raises(YouTubeRateLimited) as ei:
        YouTubeClient().list_videos(["abc"])
    assert not isinstance(ei.value, YouTubeQuotaExhausted)


def test_the_two_reason_sets_are_disjoint_and_neither_is_empty():
    """Pins MEMBERSHIP, not just the partition.

    Emptying one set makes the parametrised tests above collect ZERO cases,
    which reports as a skip rather than a failure — a vacuous pass. That exact
    mutation survived in the music copy until this assertion was added.
    """
    assert "quotaExceeded" in DAILY_QUOTA_REASONS
    assert "dailyLimitExceeded" in DAILY_QUOTA_REASONS
    assert "rateLimitExceeded" in RATE_LIMIT_REASONS
    assert "userRateLimitExceeded" in RATE_LIMIT_REASONS
    assert not (DAILY_QUOTA_REASONS & RATE_LIMIT_REASONS)


def test_a_non_quota_403_stays_generic(monkeypatch):
    """Control: mapping every 403 to "quota" would pass all of the above and
    tell a member to come back tomorrow for a permanently broken key."""
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(
        403, {"error": {"errors": [{"reason": "accessNotConfigured"}]}}))
    with pytest.raises(YouTubeError) as ei:
        YouTubeClient().list_videos(["abc"])
    assert not isinstance(ei.value, (YouTubeQuotaExhausted, YouTubeRateLimited))


@pytest.mark.parametrize("reason", [{"nested": "object"}, ["list"], 42])
def test_a_non_string_error_reason_is_not_unhashable(monkeypatch, reason):
    """`reason in DAILY_QUOTA_REASONS` raises TypeError on an unhashable value.

    `_error_reason` is documented "never raises" and does not — but the promise
    is only useful if the RETURN TYPE is guaranteed too.
    """
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(
        403, {"error": {"errors": [{"reason": reason}]}}))
    with pytest.raises(YouTubeError) as ei:
        YouTubeClient().list_videos(["abc"])
    assert not isinstance(ei.value, (YouTubeQuotaExhausted, YouTubeRateLimited))


# --------------------------------------------------------------------------
# Malformed upstream payloads → 502, never 500
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body,label", [
    ({"items": [None, {"id": "ok"}]}, "a null inside items"),
    ({"items": None}, "items is null"),
    ({}, "items absent"),
    ([1, 2, 3], "the body is a list (an edge error page parsed as JSON)"),
    ("nope", "the body is a string"),
])
def test_malformed_success_bodies_never_escape_as_a_500(monkeypatch, body, label):
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(200, body))
    try:
        YouTubeClient().list_videos(["abc"])
    except YouTubeError:
        pass  # a typed upstream failure is the intended outcome
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"{label}: escaped as {type(e).__name__}") from e


def test_non_json_success_body_is_an_upstream_error(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(200, ValueError("not json")))
    with pytest.raises(YouTubeError):
        YouTubeClient().list_videos(["abc"])


def test_a_non_string_video_id_is_dropped_not_crashed(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(200, {"items": [
        {"id": {"weird": "object"}},
        {"id": "good"},
    ]}))
    assert list(YouTubeClient().list_videos(["good"])) == ["good"]


# --------------------------------------------------------------------------
# Bounds and timeouts
# --------------------------------------------------------------------------

def test_videos_list_refuses_more_than_the_api_cap(monkeypatch):
    """Exceeding 50 silently truncates the response, which the A5 job would then
    read as "these videos are gone" and mark rows dead."""
    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    with pytest.raises(ValueError):
        YouTubeClient().list_videos([f"v{i}" for i in range(VIDEOS_LIST_MAX_IDS + 1)])


def test_videos_list_short_circuits_on_empty_input(monkeypatch):
    called = {"n": 0}

    def counted(*a, **k):
        called["n"] += 1
        return _FakeResponse(200, {"items": []})

    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", counted)
    assert YouTubeClient().list_videos([]) == {}
    assert called["n"] == 0


def test_the_outbound_request_sets_an_explicit_timeout(monkeypatch):
    seen = {}

    def capture(url, params=None, headers=None, timeout=None, **k):
        seen["timeout"] = timeout
        return _FakeResponse(200, {"items": []})

    monkeypatch.setattr("app.core.config.settings.YOUTUBE_API_KEY", "k", raising=False)
    monkeypatch.setattr("httpx.get", capture)
    YouTubeClient().list_videos(["abc"])
    assert seen["timeout"] is not None and seen["timeout"] > 0


def test_the_timeout_fits_inside_the_lambda_ceiling():
    """`ratemymusic-api` runs on the shared Lambda timeout.

    One outbound call here, plus the DB reads either side and a cold start, must
    fit — otherwise a slow-but-not-failing YouTube kills the function before the
    timeout fires and the 429/503/502 taxonomy never runs.
    """
    from app.core.config import settings
    assert 0 < settings.YOUTUBE_HTTP_TIMEOUT <= 5
