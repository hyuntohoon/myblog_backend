from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit
import os

class Settings(BaseSettings):
    # 기본값 (로컬 개발용)
    DATABASE_URL: str = "postgresql+psycopg://blog:1234@127.0.0.1:5432/blog"
    APP_NAME: str = "Blog Backend"
    ENV: str = "local"

    model_config = SettingsConfigDict(
        env_file=".env",                # 로컬에서는 .env 로드
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

def _mask(url: str) -> str:
    """비밀번호 부분 마스킹"""
    try:
        sp = urlsplit(url)
        netloc = sp.hostname or ""
        if sp.username:
            netloc = f"{sp.username}:****@{sp.hostname}:{sp.port or ''}"
        return urlunsplit((sp.scheme, netloc, sp.path, sp.query, sp.fragment))
    except Exception:
        return url

@lru_cache
def get_settings() -> Settings:
    # Lambda 환경에서는 .env를 안 읽고, 환경변수를 직접 사용
    env_database_url = os.getenv("DATABASE_URL")
    env_env = os.getenv("APP_ENV")

    s = Settings()

    if env_database_url:
        s.DATABASE_URL = env_database_url
    if env_env:
        s.ENV = env_env

    print(f"[config] ENV = {s.ENV}")
    print(f"[config] DATABASE_URL = {_mask(s.DATABASE_URL)}")
    return s

# 전역 인스턴스
settings = get_settings()