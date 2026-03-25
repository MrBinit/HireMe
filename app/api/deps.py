"""Dependency providers for repositories and services."""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.runtime_config import NotificationRuntimeConfig
from app.core.runtime_config import get_runtime_config
from app.core.security import (
    AdminPrincipal,
    AuthorizationError,
    TokenValidationError,
    decode_admin_access_token,
)
from app.core.settings import get_settings
from app.infra.database import get_async_session_factory
from app.infra.s3_store import S3ObjectStore
from app.infra.smtp_email_sender import SmtpEmailSender
from app.infra.sqs_queue import SqsParseQueuePublisher
from app.repositories.application_repository import ApplicationRepository
from app.repositories.job_opening_repository import JobOpeningRepository
from app.repositories.postgres_application_repository import PostgresApplicationRepository
from app.repositories.postgres_job_opening_repository import PostgresJobOpeningRepository
from app.repositories.postgres_reference_repository import PostgresReferenceRepository
from app.repositories.reference_repository import ReferenceRepository
from app.services.application_service import ApplicationService
from app.services.admin_auth_service import AdminAuthService
from app.services.email_sender import EmailSender, NoopEmailSender
from app.services.job_opening_service import JobOpeningService
from app.services.parse_queue import NoopParseQueuePublisher, ParseQueuePublisher
from app.services.reference_service import ReferenceService
from app.services.resume_storage import ResumeStorage, S3ResumeStorage

_admin_bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_s3_store() -> S3ObjectStore:
    """Return cached S3 object store client wrapper."""

    runtime_config = get_runtime_config()
    return S3ObjectStore(config=runtime_config.s3)


@lru_cache(maxsize=1)
def get_application_repository() -> ApplicationRepository:
    """Return cached application repository instance."""

    runtime_config = get_runtime_config()
    return PostgresApplicationRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )


@lru_cache(maxsize=1)
def get_job_opening_repository() -> JobOpeningRepository:
    """Return cached job opening repository instance."""

    runtime_config = get_runtime_config()
    return PostgresJobOpeningRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )


@lru_cache(maxsize=1)
def get_resume_storage() -> ResumeStorage:
    """Return cached resume storage backend."""

    runtime_config = get_runtime_config()
    return S3ResumeStorage(
        store=get_s3_store(),
        bucket=runtime_config.s3.bucket,
        resumes_prefix=runtime_config.s3.resumes_prefix,
    )


@lru_cache(maxsize=1)
def get_parse_queue_publisher() -> ParseQueuePublisher:
    """Return parse queue publisher based on runtime config and env."""

    runtime_config = get_runtime_config()
    settings = get_settings()

    if not runtime_config.parse.use_queue:
        return NoopParseQueuePublisher()
    if runtime_config.parse.provider != "sqs":
        return NoopParseQueuePublisher()
    if not settings.sqs_parse_queue_url:
        raise RuntimeError(
            "SQS_PARSE_QUEUE_URL is required when parse.use_queue=true and provider=sqs"
        )

    return SqsParseQueuePublisher(
        queue_url=settings.sqs_parse_queue_url,
        region=runtime_config.parse.region,
        endpoint_url=settings.sqs_endpoint_url,
    )


def _build_smtp_sender(
    *,
    config: NotificationRuntimeConfig,
    settings,
) -> EmailSender:
    """Build SMTP email sender or fallback no-op when env is incomplete."""

    if not config.smtp.host:
        return NoopEmailSender()
    if not settings.smtp_username or not settings.smtp_password:
        return NoopEmailSender()
    return SmtpEmailSender(
        host=config.smtp.host,
        port=config.smtp.port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_starttls=config.smtp.use_starttls,
        use_ssl=config.smtp.use_ssl,
        sender_name=config.sender_name,
        sender_email=config.sender_email,
        subject_template=config.confirmation_subject_template,
        body_template=config.confirmation_body_template,
    )


@lru_cache(maxsize=1)
def get_email_sender() -> EmailSender:
    """Return email sender based on notification runtime config."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    config = runtime_config.notification

    if not config.enabled:
        return NoopEmailSender()
    if config.provider == "noop":
        return NoopEmailSender()
    if config.provider == "smtp":
        return _build_smtp_sender(config=config, settings=settings)
    return NoopEmailSender()


@lru_cache(maxsize=1)
def get_job_opening_service() -> JobOpeningService:
    """Return cached job opening service instance."""

    runtime_config = get_runtime_config()
    return JobOpeningService(
        repository=get_job_opening_repository(),
        config=runtime_config.job_opening,
    )


@lru_cache(maxsize=1)
def get_reference_repository() -> ReferenceRepository:
    """Return cached reference repository instance."""

    runtime_config = get_runtime_config()
    return PostgresReferenceRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )


@lru_cache(maxsize=1)
def get_application_service() -> ApplicationService:
    """Return cached application service instance."""

    runtime_config = get_runtime_config()
    return ApplicationService(
        repository=get_application_repository(),
        job_opening_repository=get_job_opening_repository(),
        config=runtime_config.application,
        resume_storage=get_resume_storage(),
        parse_config=runtime_config.parse,
        parse_queue_publisher=get_parse_queue_publisher(),
        notification_config=runtime_config.notification,
        email_sender=get_email_sender(),
    )


@lru_cache(maxsize=1)
def get_reference_service() -> ReferenceService:
    """Return cached reference service instance."""

    return ReferenceService(
        repository=get_reference_repository(),
        application_repository=get_application_repository(),
    )


def get_job_opening_service_dep() -> JobOpeningService:
    """FastAPI dependency wrapper for job opening service."""

    return get_job_opening_service()


def get_application_service_dep() -> ApplicationService:
    """FastAPI dependency wrapper for application service."""

    return get_application_service()


def get_reference_service_dep() -> ReferenceService:
    """FastAPI dependency wrapper for reference service."""

    return get_reference_service()


@lru_cache(maxsize=1)
def get_admin_auth_service() -> AdminAuthService:
    """Return admin auth service configured from env and runtime security settings."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    return AdminAuthService(
        admin_username=settings.admin_username,
        admin_password=settings.admin_password,
        admin_password_hash=settings.admin_password_hash,
        jwt_secret=settings.admin_jwt_secret,
        security_config=runtime_config.security,
    )


def get_admin_auth_service_dep() -> AdminAuthService:
    """FastAPI dependency wrapper for admin auth service."""

    return get_admin_auth_service()


def get_admin_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_admin_bearer_scheme)],
) -> AdminPrincipal:
    """Validate bearer token and return authenticated admin principal."""

    runtime_config = get_runtime_config()
    security_config = runtime_config.security

    if not security_config.enabled:
        return AdminPrincipal(
            subject="local-dev-admin",
            role=security_config.required_role,
            expires_at=None,
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = get_settings()
    if not settings.admin_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_JWT_SECRET is not configured",
        )

    try:
        return decode_admin_access_token(
            token=credentials.credentials,
            secret=settings.admin_jwt_secret,
            config=security_config,
        )
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
