"""YAML-backed runtime configuration models and loader."""

from functools import lru_cache
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.settings import get_settings


class ApiRuntimeConfig(BaseModel):
    """Public API metadata shown in OpenAPI docs."""

    title: str = "HireMe Hiring API"
    version: str = "0.1.0"
    description: str = "Async backend for job openings and candidate applications."


class JobOpeningRuntimeConfig(BaseModel):
    """Config options for job opening validation and pagination."""

    allowed_experience_levels: list[str] = Field(
        default_factory=lambda: ["intern", "junior", "mid", "senior", "staff", "principal"]
    )
    experience_range_pattern: str = r"^\d+\s*-\s*\d+\s*years?$"
    min_bullet_items: int = 1
    max_bullet_items: int = 25
    default_list_limit: int = 20
    max_list_limit: int = 100


class ApplicationRuntimeConfig(BaseModel):
    """Config options for candidate application validation."""

    allowed_resume_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".doc", ".docx"])
    allowed_resume_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/octet-stream",
        ]
    )
    max_pdf_size_mb: int = 10
    max_doc_size_mb: int = 10
    max_docx_size_mb: int = 10
    resume_chunk_size_bytes: int = 1_048_576
    default_list_limit: int = 20
    max_list_limit: int = 100
    applications_not_open_message: str = "applications have not opened yet"
    application_paused_message: str = "Sorry, applications are currently paused for this role."
    application_closed_message: str = (
        "Sorry, the application has already been closed for this role."
    )
    invalid_resume_format_message: str = "Invalid resume format. Please upload a PDF or DOCX file."
    duplicate_application_message: str = (
        "duplicate application: this email has already applied to this job opening"
    )


class StorageRuntimeConfig(BaseModel):
    """Storage backend configuration (DB metadata + S3 resumes)."""

    backend: str = "postgres"
    resume_backend: str = "s3"
    auto_create_tables: bool = True

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, value: str) -> str:
        """Allow only postgres metadata backend."""

        if value != "postgres":
            raise ValueError("storage.backend must be 'postgres'")
        return value

    @field_validator("resume_backend")
    @classmethod
    def validate_resume_backend(cls, value: str) -> str:
        """Allow only s3 resume backend."""

        if value != "s3":
            raise ValueError("storage.resume_backend must be 's3'")
        return value


class S3StorageRuntimeConfig(BaseModel):
    """S3-specific key prefixes and transfer tuning."""

    region: str = "us-east-1"
    bucket: str = "hireme-cv-bucket"
    force_path_style: bool = False
    job_openings_prefix: str = "job-openings"
    job_opening_role_index_prefix: str = "job-opening-role-index"
    applications_prefix: str = "applications"
    application_dedupe_prefix: str = "application-dedupe"
    resumes_prefix: str = "resumes"
    list_page_size: int = 1000
    upload_max_concurrency: int = 10
    upload_multipart_threshold_mb: int = 8
    upload_multipart_chunksize_mb: int = 8


class PostgresRuntimeConfig(BaseModel):
    """Postgres async engine pool and startup configuration."""

    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout_seconds: float = 30.0
    pool_recycle_seconds: int = 1800
    pool_pre_ping: bool = True
    connect_timeout_seconds: float = 10.0
    command_timeout_seconds: float = 60.0
    echo_sql: bool = False
    ssl_mode: Literal["disable", "require"] = "require"
    ssl_root_cert_path: str | None = None


class ErrorRuntimeConfig(BaseModel):
    """Global API error response configuration."""

    request_validation_message: str = "Request validation failed."
    http_error_message: str = "HTTP error occurred."
    internal_error_message: str = "An unexpected error occurred."
    status_code_map: dict[int, str] = Field(
        default_factory=lambda: {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            422: "unprocessable_entity",
            429: "rate_limited",
            504: "request_timeout",
        }
    )


class SecurityRuntimeConfig(BaseModel):
    """JWT security configuration for admin-protected routes."""

    enabled: bool = True
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    required_role: str = "admin"
    issuer: str = "hireme-backend"
    audience: str = "hireme-admin"
    access_token_exp_minutes: int = 60
    leeway_seconds: int = 30


class ParseRuntimeConfig(BaseModel):
    """Queue/backfill runtime config for background resume parsing."""

    use_queue: bool = True
    provider: Literal["sqs", "redis", "local"] = "sqs"
    region: str = "us-east-1"
    queue_name: str = "hireme-resume-parse"
    worker_concurrency: int = 20
    max_in_flight_per_worker: int = 20
    receive_batch_size: int = 10
    receive_wait_seconds: int = 20
    enqueue_timeout_seconds: float = 2.0
    fail_submission_on_enqueue_error: bool = False
    max_extracted_chars: int = 50_000
    llm_fallback_min_chars: int = 400
    max_section_lines: int = 40
    parse_timeout_seconds: float = 60.0
    visibility_timeout_seconds: int = 300
    max_receive_count: int = 5
    link_rules: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "linkedin_domains": ["linkedin.com"],
            "github_domains": ["github.com"],
            "project_domains": [
                "github.com",
                "gitlab.com",
                "bitbucket.org",
                "dev.to",
                "behance.net",
                "dribbble.com",
                "kaggle.com",
            ],
            "excluded_personal_domains": [
                "linkedin.com",
                "github.com",
                "gitlab.com",
                "bitbucket.org",
                "dev.to",
                "medium.com",
                "behance.net",
                "dribbble.com",
                "kaggle.com",
                "x.com",
                "twitter.com",
            ],
        }
    )
    section_aliases: dict[str, list[str]] = Field(default_factory=dict)


class NotificationRuntimeConfig(BaseModel):
    """Runtime config for application confirmation emails."""

    class SmtpConfig(BaseModel):
        """SMTP transport options (non-secret)."""

        host: str = "smtp.gmail.com"
        port: int = 587
        use_starttls: bool = True
        use_ssl: bool = False

    enabled: bool = True
    provider: Literal["smtp", "noop"] = "smtp"
    sender_name: str = "HireMe Team"
    sender_email: str = "no-reply@hireme.ai"
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    confirmation_subject_template: str = "Application submitted - HireMe"
    confirmation_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Thank you for applying to HireMe. Your application has been submitted.\n\n"
        "Regards,\nHireMe Team"
    )
    send_timeout_seconds: float = 5.0
    fail_submission_on_send_error: bool = False


class GoogleApiRuntimeConfig(BaseModel):
    """Non-secret Google API/OAuth metadata loaded from YAML."""

    project_id: str = ""
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    auth_provider_x509_cert_url: str = "https://www.googleapis.com/oauth2/v1/certs"


class TimeoutRuntimeConfig(BaseModel):
    """Request-timeout middleware configuration."""

    enabled: bool = True
    seconds: float = 30.0
    message: str = "request timed out"
    exempt_paths: list[str] = Field(default_factory=lambda: ["/health"])


class RateLimitRuntimeConfig(BaseModel):
    """Rate-limit middleware configuration."""

    enabled: bool = True
    window_seconds: int = 60
    max_requests: int = 60
    key_by_path: bool = True
    trust_x_forwarded_for: bool = False
    max_tracked_clients: int = 100_000
    cleanup_interval_seconds: int = 30
    message: str = "rate limit exceeded"
    exempt_paths: list[str] = Field(
        default_factory=lambda: [
            "/health",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/docs/oauth2-redirect",
        ]
    )


class SecurityHeadersRuntimeConfig(BaseModel):
    """HTTP response header hardening configuration."""

    enabled: bool = True
    x_content_type_options: str = "nosniff"
    x_frame_options: str = "DENY"
    referrer_policy: str = "strict-origin-when-cross-origin"
    content_security_policy: str = "default-src 'none'; frame-ancestors 'none'; base-uri 'self';"
    include_hsts: bool = False
    hsts_max_age_seconds: int = 31_536_000
    hsts_include_subdomains: bool = True
    hsts_preload: bool = False
    csp_exempt_paths: list[str] = Field(
        default_factory=lambda: [
            "/docs",
            "/openapi.json",
            "/redoc",
            "/docs/oauth2-redirect",
        ]
    )


class CorsRuntimeConfig(BaseModel):
    """CORS configuration for browser-based frontend clients."""

    enabled: bool = True
    allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    allow_methods: list[str] = Field(default_factory=lambda: ["GET", "POST", "PATCH", "DELETE"])
    allow_headers: list[str] = Field(
        default_factory=lambda: ["Authorization", "Content-Type", "Accept"]
    )
    allow_credentials: bool = False
    expose_headers: list[str] = Field(default_factory=list)
    max_age_seconds: int = 600


class RuntimeConfig(BaseModel):
    """Top-level runtime configuration model."""

    api: ApiRuntimeConfig = Field(default_factory=ApiRuntimeConfig)
    job_opening: JobOpeningRuntimeConfig = Field(default_factory=JobOpeningRuntimeConfig)
    application: ApplicationRuntimeConfig = Field(default_factory=ApplicationRuntimeConfig)
    storage: StorageRuntimeConfig = Field(default_factory=StorageRuntimeConfig)
    s3: S3StorageRuntimeConfig = Field(default_factory=S3StorageRuntimeConfig)
    postgres: PostgresRuntimeConfig = Field(default_factory=PostgresRuntimeConfig)
    error: ErrorRuntimeConfig = Field(default_factory=ErrorRuntimeConfig)
    security: SecurityRuntimeConfig = Field(default_factory=SecurityRuntimeConfig)
    parse: ParseRuntimeConfig = Field(default_factory=ParseRuntimeConfig)
    notification: NotificationRuntimeConfig = Field(default_factory=NotificationRuntimeConfig)
    google_api: GoogleApiRuntimeConfig = Field(default_factory=GoogleApiRuntimeConfig)
    cors: CorsRuntimeConfig = Field(default_factory=CorsRuntimeConfig)
    security_headers: SecurityHeadersRuntimeConfig = Field(
        default_factory=SecurityHeadersRuntimeConfig
    )
    timeout: TimeoutRuntimeConfig = Field(default_factory=TimeoutRuntimeConfig)
    rate_limit: RateLimitRuntimeConfig = Field(default_factory=RateLimitRuntimeConfig)


@lru_cache(maxsize=1)
def get_runtime_config() -> RuntimeConfig:
    """Load and cache runtime configuration from YAML."""

    settings = get_settings()
    application_config_path = settings.application_config_path
    database_config_path = settings.database_config_path
    parse_config_path = settings.parse_config_path
    notification_config_path = settings.notification_config_path
    google_api_config_path = settings.google_api_config_path
    s3_config_path = settings.s3_config_path

    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is not installed in the active Python environment. "
            "Install dependencies with `pip install -r requirements.txt` or run with "
            "`venv/bin/uvicorn app.main:app --reload`."
        ) from exc

    def _load_yaml(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict):
            raise RuntimeError(f"YAML config must be a mapping: {path}")
        return payload

    raw_application_config = _load_yaml(application_config_path)
    raw_database_config = _load_yaml(database_config_path)
    raw_parse_config = _load_yaml(parse_config_path)
    raw_notification_config = _load_yaml(notification_config_path)
    raw_google_api_config = _load_yaml(google_api_config_path)
    raw_s3_config = _load_yaml(s3_config_path)

    def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            existing = merged.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                merged[key] = _merge_dicts(existing, value)
            else:
                merged[key] = value
        return merged

    combined_config = _merge_dicts(raw_application_config, raw_database_config)
    if "parse" in raw_parse_config and isinstance(raw_parse_config["parse"], dict):
        combined_config = _merge_dicts(combined_config, {"parse": raw_parse_config["parse"]})
    else:
        combined_config = _merge_dicts(combined_config, {"parse": raw_parse_config})
    if "notification" in raw_notification_config and isinstance(
        raw_notification_config["notification"], dict
    ):
        combined_config = _merge_dicts(
            combined_config,
            {"notification": raw_notification_config["notification"]},
        )
    else:
        combined_config = _merge_dicts(combined_config, {"notification": raw_notification_config})
    if "s3" in raw_s3_config and isinstance(raw_s3_config["s3"], dict):
        combined_config = _merge_dicts(combined_config, {"s3": raw_s3_config["s3"]})
    else:
        combined_config = _merge_dicts(combined_config, {"s3": raw_s3_config})
    if "google_api" in raw_google_api_config and isinstance(
        raw_google_api_config["google_api"], dict
    ):
        combined_config = _merge_dicts(
            combined_config,
            {"google_api": raw_google_api_config["google_api"]},
        )
    else:
        combined_config = _merge_dicts(combined_config, {"google_api": raw_google_api_config})

    return RuntimeConfig.model_validate(combined_config)
