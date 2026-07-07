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
    ENV: str = "local"

    # Secrets Manager (legacy) + SSM Parameter Store (CHORE-secrets-ssm-migration).
    # SECRETS_PARAM (an SSM SecureString name like /myblog/backend) takes priority;
    # SECRETS_ARN is the fallback. Setting SECRETS_PARAM is the per-service cutover
    # switch (owner Terraform env flip); unsetting it reverts to Secrets Manager.
    SECRETS_ARN: str = ""
    SECRETS_PARAM: str = ""

    # Auth / security
    EDGE_SECRET: str = ""
    ALLOW_PUBLIC_HEALTH: bool = True

    # CORS
    FRONT_ORIGIN: str = "http://localhost:4321"

    # Cognito JWT
    COGNITO_REGION: str = "ap-northeast-2"
    COGNITO_USER_POOL_ID: str = ""

    # FEAT-multi-user-accounts 0d: the owner's Cognito sub. DELETE /api/me
    # refuses this sub (403) so the blog-admin identity can't self-delete via the
    # member flow. Empty (guard off) until the owner sub lands in the Lambda env
    # via Terraform — set it alongside the 0c infra step.
    OWNER_SUB: str = ""

    # AWS / SQS — FEAT-member-dashboard Step 3 manual "지금 새로고침" trigger.
    # The backend only *produces* one message ({"job":"spotify_refresh"}); the
    # worker consumes it and does the Spotify read (rule #9 — never sync here).
    AWS_DEFAULT_REGION: str = "ap-northeast-2"
    LOCALSTACK_ENDPOINT: str | None = None
    SQS_QUEUE_URL: str = ""

    # Spotify connection status (refresh-token presence) — myblog/spotify. Read on
    # demand by the 연동 tab; the token itself is only ever used by the worker.
    # SPOTIFY_SECRETS_PARAM (SSM) takes priority over SPOTIFY_SECRETS_ARN (SM).
    SPOTIFY_SECRETS_ARN: str = ""
    SPOTIFY_SECRETS_PARAM: str = ""

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

    # FEAT-spotify-library-sync: read-only MIRROR of the worker's write gate, used
    # only to drive the /profile UI banner ("검토 모드: Spotify에 실제 반영 안 됨").
    # The backend NEVER writes to Spotify (rule #9) — the worker reads its OWN copy
    # of this flag to decide whether to issue real PUT/DELETE /me/albums; a stray
    # message can't force a write. Keep this in sync with the worker setting.
    SPOTIFY_LIBRARY_WRITES_ENABLED: bool = False

    # GitHub (loaded from Secrets Manager in prod)
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


def _load_secrets(param: str, arn: str) -> dict:
    """Load the secret JSON dict, preferring SSM Parameter Store (``param``) and
    falling back to Secrets Manager (``arn``) on unset-or-error. The env-var
    presence is the migration cutover switch (CHORE-secrets-ssm-migration)."""
    if param:
        try:
            import boto3
            ssm = boto3.client("ssm")
            val = ssm.get_parameter(Name=param, WithDecryption=True)
            return json.loads(val["Parameter"]["Value"])
        except Exception as e:
            logger.error("SSM load failed for %s, falling back to Secrets Manager: %s", param, e)
    if arn:
        try:
            import boto3
            sm = boto3.client("secretsmanager")
            val = sm.get_secret_value(SecretId=arn)
            return json.loads(val["SecretString"])
        except Exception as e:
            logger.error("Failed to load secrets from %s: %s", arn, e)
    return {}


@lru_cache
def get_settings() -> Settings:
    s = Settings()

    if s.SECRETS_ARN or s.SECRETS_PARAM:
        secrets = _load_secrets(s.SECRETS_PARAM, s.SECRETS_ARN)
        if secrets.get("DATABASE_URL"):
            s.DATABASE_URL = secrets["DATABASE_URL"]
        if secrets.get("EDGE_SECRET"):
            s.EDGE_SECRET = secrets["EDGE_SECRET"]
        if secrets.get("GITHUB_TOKEN"):
            s.GITHUB_TOKEN = secrets["GITHUB_TOKEN"]

    logger.debug("ENV=%s DATABASE_URL=%s", s.ENV, _mask(s.DATABASE_URL))
    return s


settings = get_settings()
