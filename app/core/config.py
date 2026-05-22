from __future__ import annotations

import json
import logging
import os
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

    # Secrets Manager
    SECRETS_ARN: str = ""

    # Auth / security
    EDGE_SECRET: str = ""
    ALLOW_PUBLIC_HEALTH: bool = True

    # CORS
    FRONT_ORIGIN: str = "http://localhost:4321"

    # GitHub (loaded from Secrets Manager in prod)
    GITHUB_TOKEN: str = ""

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


def _load_secrets(arn: str) -> dict:
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

    if s.SECRETS_ARN:
        secrets = _load_secrets(s.SECRETS_ARN)
        if secrets.get("DATABASE_URL"):
            s.DATABASE_URL = secrets["DATABASE_URL"]
        if secrets.get("GITHUB_TOKEN"):
            s.GITHUB_TOKEN = secrets["GITHUB_TOKEN"]

    logger.debug("ENV=%s DATABASE_URL=%s", s.ENV, _mask(s.DATABASE_URL))
    return s


settings = get_settings()
