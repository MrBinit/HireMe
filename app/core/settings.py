"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/hireme.db"
    sqs_parse_queue_url: str | None = None
    sqs_evaluation_queue_url: str | None = None
    sqs_endpoint_url: str | None = None
    api_platform_config_path: Path = Path("app/config/api_platform_config.yaml")
    application_config_path: Path = Path("app/config/application_config.yaml")
    database_config_path: Path = Path("app/config/database_config.yaml")
    parse_config_path: Path = Path("app/config/parse_config.yaml")
    notification_config_path: Path = Path("app/config/notification_config.yaml")
    google_api_config_path: Path = Path("app/config/google_api.yaml")
    s3_config_path: Path = Path("app/config/s3_config.yaml")
    bedrock_config_path: Path = Path("app/config/bedrock_config.yaml")
    evaluation_config_path: Path = Path("app/config/evaluation_config.yaml")
    smtp_username: str | None = None
    smtp_password: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    bedrock_endpoint_url: str | None = None
    admin_jwt_secret: str | None = None
    admin_username: str | None = None
    admin_password: str | None = None
    admin_password_hash: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()
