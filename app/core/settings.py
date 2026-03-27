"""Environment-backed application settings."""

from functools import lru_cache
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


USE_AWS_SECRETS_MANAGER = os.getenv("USE_AWS_SECRETS_MANAGER", "false").lower() == "true"
ENV_FILE = None if USE_AWS_SECRETS_MANAGER else ".env"


class Settings(BaseSettings):
    """Runtime settings loaded from `.env`."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/hireme.db"
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
    research_config_path: Path = Path("app/config/research_config.yaml")
    scheduling_config_path: Path = Path("app/config/scheduling_config.yaml")
    prompt_config_path: Path = Path("app/config/prompts.yaml")
    template_config_path: Path = Path("app/config/templates.yaml")
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
    referee_username: str | None = None
    referee_password: str | None = None
    referee_password_hash: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    google_service_account_json: str | None = None
    google_service_account_file: str | None = None
    interview_confirmation_token_secret: str | None = None
    serpapi_api_key: str | None = None
    github_api_token: str | None = None
    twitter_consumer_key: str | None = None
    twitter_consumer_secret: str | None = None
    twitter_bearer_token: str | None = None
    fireflies_api_key: str | None = None
    fireflies_webhook_secret: str | None = None
    docusign_access_token: str | None = None
    docusign_integration_key: str | None = None
    docusign_user_id: str | None = None
    docusign_private_key: str | None = None
    docusign_private_key_path: str | None = None
    docusign_webhook_secret: str | None = None
    slack_bot_token: str | None = None
    slack_admin_user_token: str | None = None
    slack_signing_secret: str | None = None
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    slack_bot_refresh_token: str | None = None
    slack_admin_refresh_token: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()
