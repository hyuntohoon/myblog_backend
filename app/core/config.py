# app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

class Settings(BaseSettings):
    # .env에서 DATABASE_URL을 읽습니다. (없으면 기본값 사용)
    DATABASE_URL: str = "postgresql+psycopg://blog:1234@127.0.0.1:5432/blog"
    APP_NAME: str = "Blog Backend"
    ENV: str = "local"

    # 프로젝트 루트의 .env를 읽도록 설정 (루트에 두었다면 그대로 OK)
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

@lru_cache
def get_settings() -> Settings:
    s = Settings()
    print(f"[config] DATABASE_URL = {_mask(s.DATABASE_URL)}")
    return s

# 편하게 쓰라고 모듈 전역 인스턴스도 제공합니다.
settings = get_settings()