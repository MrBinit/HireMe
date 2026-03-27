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
from app.infra.bedrock_runtime import BedrockRuntimeClient
from app.infra.google_calendar_client import GoogleCalendarClient
from app.infra.s3_store import S3ObjectStore
from app.infra.smtp_email_sender import SmtpEmailSender
from app.infra.sqs_queue import SqsParseQueuePublisher
from app.infra.sqs_queue import SqsEvaluationQueuePublisher
from app.infra.sqs_queue import SqsResearchQueuePublisher
from app.infra.sqs_queue import SqsSchedulingQueuePublisher
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
from app.services.evaluation_queue import (
    EvaluationQueuePublisher,
    NoopEvaluationQueuePublisher,
)
from app.services.research_queue import (
    NoopResearchQueuePublisher,
    ResearchQueuePublisher,
)
from app.services.scheduling_queue import (
    NoopSchedulingQueuePublisher,
    SchedulingQueuePublisher,
)
from app.services.reference_service import ReferenceService
from app.services.resume_storage import ResumeStorage, S3ResumeStorage
from app.services.candidate_evaluation_service import CandidateEvaluationService
from app.services.interview_scheduling_service import InterviewSchedulingService
from app.services.fireflies_service import FirefliesService
from app.services.docusign_service import DocusignService
from app.services.offer_letter_service import OfferLetterService
from app.services.slack_service import SlackService
from app.services.slack_welcome_service import SlackWelcomeService

_admin_bearer_scheme = HTTPBearer(auto_error=False)


def _normalize_optional_endpoint_url(value: str | None) -> str | None:
    """Return None for blank endpoint env values to satisfy boto client validation."""

    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


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
def get_bedrock_runtime_client() -> BedrockRuntimeClient:
    """Return cached Bedrock runtime client."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    return BedrockRuntimeClient(
        region=runtime_config.bedrock.region,
        max_retries=runtime_config.bedrock.max_retries,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        aws_session_token=settings.aws_session_token,
        endpoint_url=_normalize_optional_endpoint_url(settings.bedrock_endpoint_url),
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
    if not runtime_config.parse.queue_url:
        raise RuntimeError("parse.queue_url is required when parse.use_queue=true and provider=sqs")

    return SqsParseQueuePublisher(
        queue_url=runtime_config.parse.queue_url,
        region=runtime_config.parse.region,
        endpoint_url=_normalize_optional_endpoint_url(settings.sqs_endpoint_url),
    )


@lru_cache(maxsize=1)
def get_evaluation_queue_publisher() -> EvaluationQueuePublisher:
    """Return evaluation queue publisher based on runtime config and env."""

    runtime_config = get_runtime_config()
    settings = get_settings()

    if not runtime_config.evaluation.use_queue:
        return NoopEvaluationQueuePublisher()
    if runtime_config.evaluation.provider != "sqs":
        return NoopEvaluationQueuePublisher()
    if not runtime_config.evaluation.queue_url:
        raise RuntimeError(
            "evaluation.queue_url is required when evaluation.use_queue=true and provider=sqs"
        )

    return SqsEvaluationQueuePublisher(
        queue_url=runtime_config.evaluation.queue_url,
        region=runtime_config.evaluation.region,
        endpoint_url=_normalize_optional_endpoint_url(settings.sqs_endpoint_url),
    )


@lru_cache(maxsize=1)
def get_research_queue_publisher() -> ResearchQueuePublisher:
    """Return research queue publisher based on runtime config and env."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    enrichment = runtime_config.research.enrichment

    if not enrichment.use_queue:
        return NoopResearchQueuePublisher()
    if enrichment.provider != "sqs":
        return NoopResearchQueuePublisher()
    if not enrichment.queue_url:
        raise RuntimeError(
            "research.enrichment.queue_url is required when "
            "research.enrichment.use_queue=true and provider=sqs"
        )

    return SqsResearchQueuePublisher(
        queue_url=enrichment.queue_url,
        region=enrichment.region,
        endpoint_url=_normalize_optional_endpoint_url(settings.sqs_endpoint_url),
    )


@lru_cache(maxsize=1)
def get_scheduling_queue_publisher() -> SchedulingQueuePublisher:
    """Return scheduling queue publisher based on runtime config and env."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    scheduling = runtime_config.scheduling

    if not scheduling.use_queue:
        return NoopSchedulingQueuePublisher()
    if scheduling.provider != "sqs":
        return NoopSchedulingQueuePublisher()
    if not scheduling.queue_url:
        raise RuntimeError(
            "scheduling.queue_url is required when " "scheduling.use_queue=true and provider=sqs"
        )

    return SqsSchedulingQueuePublisher(
        queue_url=scheduling.queue_url,
        region=scheduling.region,
        endpoint_url=_normalize_optional_endpoint_url(settings.sqs_endpoint_url),
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
        confirmation_subject_template=config.confirmation_subject_template,
        confirmation_body_template=config.confirmation_body_template,
        rejection_subject_template=config.rejection_subject_template,
        rejection_body_template=config.rejection_body_template,
        interview_options_subject_template=config.interview_options_subject_template,
        interview_options_body_template=config.interview_options_body_template,
        interview_reminder_subject_template=config.interview_reminder_subject_template,
        interview_reminder_body_template=config.interview_reminder_body_template,
        interview_confirmed_subject_template=config.interview_confirmed_subject_template,
        interview_confirmed_body_template=config.interview_confirmed_body_template,
        interview_thank_you_subject_template=config.interview_thank_you_subject_template,
        interview_thank_you_body_template=config.interview_thank_you_body_template,
        interview_reschedule_options_subject_template=(
            config.interview_reschedule_options_subject_template
        ),
        interview_reschedule_options_body_template=config.interview_reschedule_options_body_template,
        offer_letter_subject_template=config.offer_letter_subject_template,
        offer_letter_body_template=config.offer_letter_body_template,
        manager_rejection_subject_template=config.manager_rejection_subject_template,
        manager_rejection_body_template=config.manager_rejection_body_template,
        offer_signed_alert_subject_template=config.offer_signed_alert_subject_template,
        offer_signed_alert_body_template=config.offer_signed_alert_body_template,
        slack_invite_subject_template=config.slack_invite_subject_template,
        slack_invite_body_template=config.slack_invite_body_template,
        slack_joined_alert_subject_template=config.slack_joined_alert_subject_template,
        slack_joined_alert_body_template=config.slack_joined_alert_body_template,
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
        offer_letter_service=get_offer_letter_service(),
        docusign_service=get_docusign_service(),
        slack_service=get_slack_service(),
        slack_welcome_service=get_slack_welcome_service(),
        s3_store=get_s3_store(),
        s3_bucket=runtime_config.s3.bucket,
    )


@lru_cache(maxsize=1)
def get_offer_letter_service() -> OfferLetterService:
    """Return cached secondary-model-backed offer-letter generator."""

    runtime_config = get_runtime_config()
    return OfferLetterService(
        bedrock_client=get_bedrock_runtime_client(),
        bedrock_config=runtime_config.bedrock,
        evaluation_config=runtime_config.evaluation,
    )


@lru_cache(maxsize=1)
def get_docusign_service() -> DocusignService | None:
    """Return cached DocuSign service when configured and enabled."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    service = DocusignService(
        config=runtime_config.docusign,
        access_token=settings.docusign_access_token,
        integration_key=settings.docusign_integration_key,
        user_id=settings.docusign_user_id,
        private_key=settings.docusign_private_key,
        private_key_path=settings.docusign_private_key_path,
        webhook_secret=settings.docusign_webhook_secret,
    )
    if not service.enabled:
        return None
    return service


@lru_cache(maxsize=1)
def get_slack_service() -> SlackService | None:
    """Return cached Slack service when configured and enabled."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    service = SlackService(
        config=runtime_config.slack,
        bot_token=settings.slack_bot_token,
        admin_user_token=settings.slack_admin_user_token,
        signing_secret=settings.slack_signing_secret,
        client_id=settings.slack_client_id,
        client_secret=settings.slack_client_secret,
        bot_refresh_token=settings.slack_bot_refresh_token,
        admin_refresh_token=settings.slack_admin_refresh_token,
    )
    if not service.enabled:
        return None
    return service


@lru_cache(maxsize=1)
def get_slack_welcome_service() -> SlackWelcomeService | None:
    """Return cached Slack welcome generator when enabled."""

    runtime_config = get_runtime_config()
    if not runtime_config.slack.enabled:
        return None
    return SlackWelcomeService(
        bedrock_client=get_bedrock_runtime_client(),
        bedrock_config=runtime_config.bedrock,
        evaluation_config=runtime_config.evaluation,
    )


@lru_cache(maxsize=1)
def get_candidate_evaluation_service() -> CandidateEvaluationService:
    """Return cached candidate evaluation service backed by Bedrock."""

    runtime_config = get_runtime_config()
    return CandidateEvaluationService(
        application_repository=get_application_repository(),
        job_opening_repository=get_job_opening_repository(),
        bedrock_client=get_bedrock_runtime_client(),
        bedrock_config=runtime_config.bedrock,
        evaluation_config=runtime_config.evaluation,
        application_config=runtime_config.application,
    )


@lru_cache(maxsize=1)
def get_interview_scheduling_service() -> InterviewSchedulingService:
    """Return cached interview scheduling service."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    return InterviewSchedulingService(
        application_repository=get_application_repository(),
        job_opening_repository=get_job_opening_repository(),
        calendar_client=GoogleCalendarClient(
            service_account_json=settings.google_service_account_json,
            service_account_file=settings.google_service_account_file,
            oauth_client_id=settings.google_client_id,
            oauth_client_secret=settings.google_client_secret,
            oauth_refresh_token=settings.google_refresh_token,
            oauth_token_uri=runtime_config.google_api.token_uri,
        ),
        email_sender=get_email_sender(),
        config=runtime_config.scheduling,
        security_config=runtime_config.security,
        confirmation_token_secret=(
            settings.interview_confirmation_token_secret or settings.admin_jwt_secret
        ),
        fireflies_service=get_fireflies_service(),
    )


@lru_cache(maxsize=1)
def get_fireflies_service() -> FirefliesService:
    """Return cached Fireflies API service used by interview orchestration workers."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    return FirefliesService(
        api_key=settings.fireflies_api_key,
        config=runtime_config.scheduling.fireflies,
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


def get_candidate_evaluation_service_dep() -> CandidateEvaluationService:
    """FastAPI dependency wrapper for candidate evaluation service."""

    return get_candidate_evaluation_service()


def get_interview_scheduling_service_dep() -> InterviewSchedulingService:
    """FastAPI dependency wrapper for interview scheduling service."""

    return get_interview_scheduling_service()


def get_evaluation_queue_publisher_dep() -> EvaluationQueuePublisher:
    """FastAPI dependency wrapper for evaluation queue publisher."""

    return get_evaluation_queue_publisher()


def get_research_queue_publisher_dep() -> ResearchQueuePublisher:
    """FastAPI dependency wrapper for research queue publisher."""

    return get_research_queue_publisher()


def get_scheduling_queue_publisher_dep() -> SchedulingQueuePublisher:
    """FastAPI dependency wrapper for scheduling queue publisher."""

    return get_scheduling_queue_publisher()


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
        auth_role=runtime_config.security.required_role,
        auth_label="admin",
    )


@lru_cache(maxsize=1)
def get_referee_auth_service() -> AdminAuthService:
    """Return referee auth service configured from env and runtime security settings."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    return AdminAuthService(
        admin_username=settings.referee_username,
        admin_password=settings.referee_password,
        admin_password_hash=settings.referee_password_hash,
        jwt_secret=settings.admin_jwt_secret,
        security_config=runtime_config.security,
        auth_role="referee",
        auth_label="referee",
    )


def get_admin_auth_service_dep() -> AdminAuthService:
    """FastAPI dependency wrapper for admin auth service."""

    return get_admin_auth_service()


def get_referee_auth_service_dep() -> AdminAuthService:
    """FastAPI dependency wrapper for referee auth service."""

    return get_referee_auth_service()


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
            required_role=security_config.required_role,
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


def get_referee_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_admin_bearer_scheme)],
) -> AdminPrincipal:
    """Validate bearer token and return authenticated referee principal."""

    runtime_config = get_runtime_config()
    security_config = runtime_config.security

    if not security_config.enabled:
        return AdminPrincipal(
            subject="local-dev-referee",
            role="referee",
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
            required_role="referee",
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
