from __future__ import annotations

import json
import logging
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+psycopg://blog:1234@127.0.0.1:5432/blog"

    # App
    APP_NAME: str = "Blog Backend"
    # SEC-2 (OPS-safety-net-drift Step 3): absence must be restrictive. ENV
    # gates every local-dev permissiveness (auth/edge-guard bypass, CORS
    # localhost origins, FastAPI debug, owner live-tail) — with a "local"
    # default, a Lambda that ever lost its ENV var would silently disable ALL
    # auth. Local dev opts in explicitly via ENV=local (repo README).
    ENV: str = "prod"

    # Runtime secrets: SSM Parameter Store ONLY (CHORE-secrets-ssm-migration).
    # SECRETS_PARAM is an SSM SecureString name like /myblog/backend. The legacy
    # Secrets Manager fallback (SECRETS_ARN) was removed once the migration
    # completed — AWS Secrets Manager holds zero secrets in this account, so the
    # fallback could only ever turn an SSM failure into a silent empty load.
    SECRETS_PARAM: str = ""

    # Auth / security
    EDGE_SECRET: str = ""
    ALLOW_PUBLIC_HEALTH: bool = True

    # CORS
    FRONT_ORIGIN: str = "http://localhost:4321"

    # Cognito JWT
    COGNITO_REGION: str = "ap-northeast-2"
    COGNITO_USER_POOL_ID: str = ""
    # SEC-system-hardening: Cognito app clients whose tokens this service accepts,
    # comma-separated. Empty is a MISCONFIGURATION and fails closed (503), never
    # "accept any client in the pool" — see app/core/auth.py. Set from
    # infra/lambda.tf so a client can be added or retired without a code deploy.
    COGNITO_ALLOWED_CLIENT_IDS: str = ""

    # FEAT-multi-user-accounts 0d: the owner's Cognito sub. DELETE /api/me
    # refuses this sub (403) so the blog-admin identity can't self-delete via the
    # member flow. Empty (guard off) until the owner sub lands in the Lambda env
    # via Terraform — set it alongside the 0c infra step.
    OWNER_SUB: str = ""

    # FIX-nightly-draft-identity Phase A: the Cognito sub of the nightly draft
    # agent (scripts/buckit_nightly.py). It is accepted by
    # require_owner_or_draft_agent on draft creation ONLY, and create_post
    # coerces its posts to status='draft' so it can never publish.
    # Empty means "no agent exists" and must degrade to owner-only — never a
    # wildcard. require_owner (38 routes) is deliberately NOT widened.
    DRAFT_AGENT_SUB: str = ""

    # FEAT-multi-user-accounts Phase 1: anti-abuse floor for public album reviews.
    # A member may CREATE at most this many reviews per rolling 24h window (edits
    # to an existing review don't count). Generous enough to be invisible to real
    # use (Gate G1 needs only ~10 total) while stopping scripted mass-rating.
    REVIEW_DAILY_CAP: int = 50
    # Same rolling-24h anti-abuse pattern extended to the other member-writable
    # creates (2026-07-14). Items count ROWS (artist source-expansion adds its
    # whole batch), checked before insert. Integrations connects are NOT capped:
    # user_integrations upserts one row per (user_id, provider), so there is no
    # attempt trail to count without a schema change.
    BUCKET_DAILY_CAP: int = 30
    BUCKET_ITEM_DAILY_CAP: int = 500
    # FEAT-personal-release-tracking Step 2: newly tracked artist edges per
    # member in a rolling 24-hour window. Existing edges skipped by the bulk
    # upsert do not consume the cap.
    TRACKED_ARTIST_DAILY_CAP: int = 500

    # DATA-catalog-noise-and-lyrics-coverage Step 2: read-side classical-compilation
    # filter tunables for GET /api/me/release-feed (app/services/compilation_filter.py).
    # A feed item is dropped if its resolved catalog album credits
    # >= COMP_FILTER_MAX_ARTISTS distinct artists, OR carries a pure-compilation
    # label, OR matches a compilation title family; announced-only items (no
    # catalog album) fall back to the title signal. Read-side only — reversible.
    # Twin of myblog_music/app/core/config.py: names + defaults must match (a fix
    # lands in both repos). The owner already tracks Claude Debussy + Michael
    # Korstick, so this gate is live the moment a member follows a classical artist.
    COMP_FILTER_MAX_ARTISTS: int = 10
    COMP_FILTER_BUDGET_LABELS: list[str] = [
        "UME - Global Clearing House",
        "Novus Promusica",
        "Naxos Special Projects",
    ]

    # AWS / SQS — FEAT-member-dashboard Step 3 manual "지금 새로고침" trigger.
    # The backend only *produces* one message ({"job":"spotify_refresh"}); the
    # worker consumes it and does the Spotify read (rule #9 — never sync here).
    AWS_DEFAULT_REGION: str = "ap-northeast-2"
    LOCALSTACK_ENDPOINT: str | None = None
    SQS_QUEUE_URL: str = ""

    # Spotify connection status (refresh-token presence) — myblog/spotify. Read on
    # demand by the 연동 tab; the token itself is only ever used by the worker.
    # SSM SecureString name only (CHORE-secrets-ssm-migration).
    SPOTIFY_SECRETS_PARAM: str = ""

    # YouTube Data API v3 (FEAT-youtube-playback-provider Step A3).
    # `videos.list` ONLY here — discovery (`search.list`, 100 units) lives in
    # myblog_music and must not gain a second home. Backend calls this to VERIFY
    # a videoId before writing it into the GLOBAL mapping table: if the write
    # trusted the request body, one member could poison a row every other member
    # resolves.
    #
    # Its own SSM parameter, shared with music and (at A5) worker — one key, one
    # home, one rotation. Deliberately NOT in REQUIRED_SECRET_KEYS: backend must
    # keep booting when YouTube is unconfigured, and the mapping routes fail
    # closed with 503 on their own. An absent credential must never widen
    # anything, and it must not take the site down either.
    YOUTUBE_SECRETS_PARAM: str = ""
    YOUTUBE_API_KEY: str = ""
    YOUTUBE_API_BASE: str = "https://www.googleapis.com/youtube/v3"
    # ONE call per request here (unlike music's two), but the same reasoning: it
    # must fit inside the Lambda timeout with room for the DB work either side.
    YOUTUBE_HTTP_TIMEOUT: float = 4.0

    # FEAT-pocket-buckit Step 3 (D3 / OQ8): the async Spotify Web Playback SDK token mint
    # (GET /api/playback/spotify-token) exchanges a per-listener `streaming`-scope refresh
    # token for a short-lived access token. These are read from the myblog/spotify secret
    # at mint time (PlaybackService); the env vars are local/test overrides only. The
    # streaming refresh token is DISTINCT from the worker's read-only refresh_token (which
    # carries no `streaming` scope) and is EMPTY until the owner completes the Step-5
    # `streaming` OAuth consent — until then the endpoint returns 503 not-configured, so no
    # Spotify call ever fires (rule #9-safe by construction). Never logged.
    SPOTIFY_CLIENT_ID: str = ""
    SPOTIFY_CLIENT_SECRET: str = ""
    SPOTIFY_STREAMING_REFRESH_TOKEN: str = ""

    # FEAT-multi-user-accounts 3b-c: member Spotify connect (server-side code
    # exchange). The front callback page captures `?code` and PUTs it authed; the
    # exchange posts this exact redirect_uri (must match the Spotify dashboard
    # registration byte-for-byte or the exchange 400s).
    SPOTIFY_MEMBER_REDIRECT_URI: str = (
        "https://www.ratemymusic.blog/settings/spotify/callback"
    )
    # KMS key (key id / ARN / alias, e.g. 'alias/myblog-user-tokens') that envelopes
    # member Spotify refresh tokens stored in user_integrations.payload. EMPTY ⇒ the
    # connect endpoint 503s fail-closed BEFORE any Spotify call — plaintext is never
    # stored and the one-time code is not burned (3a LASTFM_API_KEY dormant
    # precedent; the 3b-a CMK awaits the owner's terraform apply).
    USER_TOKENS_KMS_KEY_ID: str = ""

    # FEAT-spotify-library-sync: read-only MIRROR of the worker's write gate, used
    # only to drive the /profile UI banner ("검토 모드: Spotify에 실제 반영 안 됨").
    # The backend NEVER writes to Spotify (rule #9) — the worker reads its OWN copy
    # of this flag to decide whether to issue real PUT/DELETE /me/albums; a stray
    # message can't force a write. Keep this in sync with the worker setting.
    SPOTIFY_LIBRARY_WRITES_ENABLED: bool = False

    # GitHub (loaded from the SSM secret in prod)
    GITHUB_TOKEN: str = ""
    GITHUB_REPO_OWNER: str = ""
    GITHUB_REPO_NAME: str = ""
    GITHUB_REPO_BRANCH: str = "main"
    CONTENT_DIR: str = "content/blog"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


def _mask(url: str) -> str:
    try:
        sp = urlsplit(url)
        netloc = sp.hostname or ""
        if sp.username:
            netloc = f"{sp.username}:****@{sp.hostname}:{sp.port or ''}"
        return urlunsplit((sp.scheme, netloc, sp.path, sp.query, sp.fragment))
    except Exception:
        return url


# --- Required-secret contract for the /myblog/backend SecureString -----------
#
# Derived from the runtime call sites, not from what the parameter happens to
# contain today. A key is REQUIRED only when its absence breaks the service as a
# whole; a key whose absence breaks one feature that already fails loudly at its
# own call site is deliberately left optional, because raising here takes the
# entire Lambda down — every read route included — to punish a partial outage.
#
# DATABASE_URL — `app/db/session.py` builds the engine at import time. Settings
#   carries a *localhost* default for local dev, so an absent key does not
#   surface as an empty string: the Lambda boots pointed at 127.0.0.1 and every
#   `/api` route dies at request time with a connection error naming a host that
#   does not exist in Lambda and no mention of SSM.
# EDGE_SECRET — `app/main.py:edge_guard` requires it truthy before it will trust
#   CloudFront's injected `x-origin-verify`. Empty is already fail-*closed* (the
#   request falls through to the raw-invoke branch and 403s, it never fails
#   open), but every CloudFront request — i.e. the whole site — 403s with a
#   "Forbidden" that names nothing.
#
# GITHUB_TOKEN is NOT required, on purpose: it feeds three owner-only write
# paths, none of them on a read path, and a rotated-out token must not take the
# public API offline. Be precise about what "degraded" costs, because it is not
# uniform and only the first of the three is loud:
#   - POST /api/posts (`app/api/routes/publish.py`) returns 500 "Missing GitHub
#     environment variables". Loud, and the caller learns nothing shipped.
#   - DELETE/archive a post (`app/api/routes/posts.py` -> `remove_post_content`)
#     returns success. `_github_config` logs and returns None, the route catches
#     only RuntimeError, so the DB row is archived or deleted while the published
#     MDX STAYS LIVE on the content repo — a takedown that takes nothing down.
#   - Restore (`app/api/routes/posts.py` -> `republish_post_content`) likewise
#     returns 200 "published" while the static page is never recreated (404).
# The two silent ones are the reason absence is logged at WARNING rather than
# passed over: the operator gets no other signal. WARNING (not INFO) because
# nothing configures a level on this function — no LOG_LEVEL env var and no
# ApplicationLogLevel in its LoggingConfig, verified against `ratemymusic-api`
# 2026-08-29 — so the runtime bootstrap's root level decides, and WARNING is
# above it under every default this runtime has shipped.
#
# COGNITO_USER_POOL_ID / COGNITO_ALLOWED_CLIENT_IDS / OWNER_SUB / DRAFT_AGENT_SUB
# are not in this parameter at all — they arrive as Lambda env vars from
# `infra/lambda.tf`, and `app/core/auth.py` already fails closed (503) on each.
# SPOTIFY_SECRETS_PARAM points at a *different* parameter (`/myblog/spotify`)
# that is read on demand per request and degrades to a 503, not at boot.
REQUIRED_SECRET_KEYS: tuple[str, ...] = ("DATABASE_URL", "EDGE_SECRET")
OPTIONAL_SECRET_KEYS: tuple[str, ...] = ("GITHUB_TOKEN",)


def _present(value: object) -> bool:
    """True when `value` is a usable secret string.

    A key written as `null`, `""`, or `"   "` is as absent as a key that is not
    there at all — SSM parameters are hand-edited JSON and all three shapes have
    a plausible way of happening. Non-strings are rejected rather than coerced:
    every field these feed is typed `str`.
    """
    return isinstance(value, str) and bool(value.strip())


def _load_secrets(param: str) -> dict:
    """Load the secret JSON dict from SSM Parameter Store (SecureString).

    SSM is the only source (CHORE-secrets-ssm-migration). A failure here is a
    misconfiguration, an IAM regression, or an SSM outage — none of which this
    process can serve through — so it is raised, not swallowed. The old code
    logged and returned ``{}``, which let the Lambda finish importing with the
    localhost DATABASE_URL default and an empty EDGE_SECRET; every request then
    failed one layer later with an error that named neither SSM nor the
    parameter.

    ``myblog_music`` and ``myblog_worker`` raise on this condition too, and as
    of this change so does the successful-load-but-key-missing condition —
    see ``_apply_secrets``. The required *set* is this service's own, not a copy
    of theirs.

    Everything that can fail is inside the ``try``: constructing the client
    (``NoRegionError``) and parsing the value (``JSONDecodeError``, which the
    `/myblog/backend` parameter has actually been written badly enough to raise
    before) are as much "the load failed" as the API call is, and each must
    still produce a log line naming the parameter.
    """
    import boto3

    try:
        ssm = boto3.client("ssm")
        val = ssm.get_parameter(Name=param, WithDecryption=True)
        return json.loads(val["Parameter"]["Value"])
    except Exception as e:
        logger.error("SSM load failed for %s: %s", param, e)
        raise


def _apply_secrets(s: Settings, secrets: object, param: str) -> None:
    """Copy the loaded secret payload onto `s`, failing closed on a missing key.

    Presence is decided from the PAYLOAD, never from the resulting attribute.
    ``DATABASE_URL`` has a non-empty localhost default, so the ``if not
    s.DATABASE_URL`` formulation that `myblog_music` and `myblog_worker` can use
    (their defaults are ``""``) is silently a no-op here — it would let a
    parameter that omits the key boot the Lambda against 127.0.0.1. Asking what
    SSM actually supplied is the only check that catches it.

    Values are assigned verbatim. Whitespace is used to judge presence but never
    stripped: ``EDGE_SECRET`` is compared byte-for-byte against the value
    CloudFront injects from this same parameter, so silently trimming it here
    would 403 the whole site to "fix" a value the edge still sends untrimmed.

    Raises ValueError naming the parameter and the missing KEYS. No secret value
    is included, and none is logged.
    """
    if not isinstance(secrets, dict):
        raise ValueError(
            f"{param} must hold a JSON object of secret keys; parsed as "
            f"{type(secrets).__name__}. Fix the SecureString value."
        )

    missing = [k for k in REQUIRED_SECRET_KEYS if not _present(secrets.get(k))]
    if missing:
        raise ValueError(
            f"Required secrets missing from {param}: {sorted(missing)}. "
            "The parameter was read and parsed as JSON, so this is not an SSM or "
            "IAM failure — these keys are absent, empty, or not strings. The "
            "process refuses to start rather than serve on defaults: "
            "DATABASE_URL would fall back to a localhost engine and EDGE_SECRET "
            "would 403 every CloudFront request."
        )

    for key in OPTIONAL_SECRET_KEYS:
        if not _present(secrets.get(key)):
            logger.warning(
                "%s has no usable %s; boot continues by design (this key is "
                "optional — requiring it would take every read route down for an "
                "owner-only feature). Degraded while it is absent: POST /api/posts "
                "returns 500, and post delete/archive and restore return SUCCESS "
                "while silently not removing or not recreating the published MDX.",
                param,
                key,
            )

    s.DATABASE_URL = secrets["DATABASE_URL"]
    s.EDGE_SECRET = secrets["EDGE_SECRET"]
    if _present(secrets.get("GITHUB_TOKEN")):
        s.GITHUB_TOKEN = secrets["GITHUB_TOKEN"]


@lru_cache
def get_settings() -> Settings:
    s = Settings()

    # Gated on SECRETS_PARAM, which only a deployed environment sets
    # (infra/lambda.tf). Local dev and the test suite leave it empty, take no
    # SSM call, and are unaffected by the required-key contract above.
    if s.SECRETS_PARAM:
        _apply_secrets(s, _load_secrets(s.SECRETS_PARAM), s.SECRETS_PARAM)

    # Loaded separately and NOT required — see YOUTUBE_SECRETS_PARAM above. The
    # failure is logged and swallowed, which is correct HERE and nowhere else in
    # this function, precisely because the fallback state is "refuse" (the
    # mapping routes 503 on an empty key) rather than "proceed on a default".
    if s.YOUTUBE_SECRETS_PARAM and not s.YOUTUBE_API_KEY:
        try:
            s.YOUTUBE_API_KEY = _load_secrets(s.YOUTUBE_SECRETS_PARAM).get("YOUTUBE_API_KEY", "")
        except Exception:
            logger.error(
                "YouTube secret load failed for %s; the mapping routes will fail closed.",
                s.YOUTUBE_SECRETS_PARAM,
            )

    logger.debug("ENV=%s DATABASE_URL=%s", s.ENV, _mask(s.DATABASE_URL))
    return s


settings = get_settings()
