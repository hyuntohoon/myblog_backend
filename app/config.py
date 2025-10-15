from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    env: str = "local"

    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_name: str = "myblog"
    db_user: str = "myblog_user"
    db_password: str = "myblog_pass"

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        extra="ignore",
    )

settings = Settings()
