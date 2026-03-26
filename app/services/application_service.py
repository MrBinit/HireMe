"""Business logic for candidate application submissions."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import anyio
from fastapi import UploadFile

from app.core.error import ApplicationValidationError
from app.core.runtime_config import (
    ApplicationRuntimeConfig,
    NotificationRuntimeConfig,
    ParseRuntimeConfig,
)
from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
)
from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.application import (
    ApplicantStatus,
    ApplicationCreatePayload,
    ApplicationRecord,
    ResumeFileMeta,
    StatusHistoryEntry,
)
from app.schemas.application import ApplicationListResponse
from app.schemas.job_opening import JobOpeningRecord
from app.services.email_sender import ApplicationConfirmationEmail, EmailSendError, EmailSender
from app.services.parse_queue import ParseQueuePublishError, ParseQueuePublisher, ResumeParseJob
from app.services.resume_storage import ResumeStorage

logger = logging.getLogger(__name__)


class ApplicationService:
    """Service layer for submitting applications and handling resume uploads."""

    def __init__(
        self,
        repository: ApplicationRepository,
        job_opening_repository: JobOpeningRepository,
        config: ApplicationRuntimeConfig,
        resume_storage: ResumeStorage,
        parse_config: ParseRuntimeConfig,
        parse_queue_publisher: ParseQueuePublisher,
        notification_config: NotificationRuntimeConfig,
        email_sender: EmailSender,
    ):
        """Initialize service with required repositories and runtime settings."""

        self._repository = repository
        self._job_opening_repository = job_opening_repository
        self._config = config
        self._resume_storage = resume_storage
        self._parse_config = parse_config
        self._parse_queue_publisher = parse_queue_publisher
        self._notification_config = notification_config
        self._email_sender = email_sender

    async def submit(
        self,
        payload: ApplicationCreatePayload,
        resume: UploadFile,
    ) -> ApplicationRecord:
        """Submit an application and persist resume metadata."""

        normalized_payload = self._normalize_payload(payload)
        opening = await self._job_opening_repository.find_by_role_title(
            normalized_payload.role_selection
        )
        if opening is None:
            raise ApplicationValidationError(
                "role_selection is not available. Create the job opening first."
            )

        now = datetime.now(tz=timezone.utc)
        if now < opening.application_open_at:
            raise ApplicationValidationError(self._config.applications_not_open_message)
        if opening.paused:
            raise ApplicationValidationError(self._config.application_paused_message)
        if now > opening.application_close_at:
            raise ApplicationValidationError(self._config.application_closed_message)

        original_filename = resume.filename or ""
        extension = Path(original_filename).suffix.lower()
        self._validate_resume_type(extension=extension, content_type=resume.content_type or "")

        app_id = uuid4()
        stored_filename = f"{app_id}{extension}"
        max_size_mb = self._resolve_max_size_mb(extension)
        max_bytes = max_size_mb * 1024 * 1024

        try:
            resume_upload = await self._resume_storage.save(
                resume=resume,
                stored_filename=stored_filename,
                content_type=resume.content_type or "application/octet-stream",
                max_bytes=max_bytes,
                chunk_size=self._config.resume_chunk_size_bytes,
            )
        except ValueError as exc:
            message = str(exc)
            if "maximum size" in message or "exceeds" in message:
                raise ApplicationValidationError(
                    f"resume file too large; max allowed is {max_size_mb} MB"
                ) from exc
            if "empty" in message:
                raise ApplicationValidationError("resume file is empty") from exc
            raise
        finally:
            await resume.close()

        created_at = datetime.now(tz=timezone.utc)
        record = ApplicationRecord(
            id=app_id,
            job_opening_id=opening.id,
            full_name=normalized_payload.full_name,
            email=normalized_payload.email,
            linkedin_url=normalized_payload.linkedin_url,
            portfolio_url=normalized_payload.portfolio_url,
            github_url=normalized_payload.github_url,
            twitter_url=normalized_payload.twitter_url,
            role_selection=opening.role_title,
            resume=ResumeFileMeta(
                original_filename=original_filename,
                stored_filename=stored_filename,
                storage_path=resume_upload.storage_path,
                content_type=resume.content_type or "application/octet-stream",
                size_bytes=resume_upload.size_bytes,
            ),
            parse_result=None,
            parsed_total_years_experience=None,
            parsed_search_text=None,
            parse_status="pending",
            evaluation_status=None,
            applicant_status="applied",
            rejection_reason=None,
            ai_score=None,
            ai_screening_summary=None,
            candidate_brief=None,
            online_research_summary=None,
            interview_schedule_status=None,
            interview_schedule_options=None,
            interview_schedule_sent_at=None,
            interview_hold_expires_at=None,
            interview_calendar_email=None,
            interview_schedule_error=None,
            status_history=[
                StatusHistoryEntry(
                    status="applied",
                    note="application submitted",
                    changed_at=created_at,
                    source="system",
                )
            ],
            reference_status=False,
            created_at=created_at,
        )

        try:
            created = await self._repository.create(record)
        except DuplicateApplicationError as exc:
            raise ApplicationValidationError(self._config.duplicate_application_message) from exc

        if self._parse_config.use_queue:
            parse_job = ResumeParseJob(
                application_id=created.id,
                job_opening_id=created.job_opening_id,
                role_selection=created.role_selection,
                email=str(created.email),
                resume_storage_path=created.resume.storage_path,
                created_at=created.created_at,
            )
            try:
                with anyio.fail_after(self._parse_config.enqueue_timeout_seconds):
                    await self._parse_queue_publisher.publish(parse_job)
            except (ParseQueuePublishError, TimeoutError):
                logger.exception(
                    "failed to enqueue parse job",
                    extra={"application_id": str(created.id)},
                )
                if self._parse_config.fail_submission_on_enqueue_error:
                    raise ApplicationValidationError(
                        "application submission failed to queue for parsing"
                    ) from None

        if self._notification_config.enabled:
            email_payload = ApplicationConfirmationEmail(
                candidate_name=created.full_name,
                candidate_email=str(created.email),
                role_title=created.role_selection,
            )
            try:
                with anyio.fail_after(self._notification_config.send_timeout_seconds):
                    await self._email_sender.send_application_confirmation(email_payload)
            except (EmailSendError, TimeoutError):
                logger.exception(
                    "failed to send application confirmation email",
                    extra={"application_id": str(created.id)},
                )
                if self._notification_config.fail_submission_on_send_error:
                    raise ApplicationValidationError(
                        "application submission email notification failed"
                    ) from None

        return created

    async def get_allowed_roles(self) -> list[str]:
        """Return role titles currently available from job openings."""

        now = datetime.now(tz=timezone.utc)
        openings, _ = await self._job_opening_repository.list(offset=0, limit=1000)
        roles = [
            item.role_title
            for item in openings
            if not item.paused and item.application_open_at <= now <= item.application_close_at
        ]
        return sorted(set(roles))

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
        job_opening_id: UUID | None = None,
        role_selection: str | None = None,
        applicant_status: ApplicantStatus | None = None,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        keyword_search: str | None = None,
        experience_within_range: bool | None = None,
        prefilter_by_job_opening: bool = False,
    ) -> ApplicationListResponse:
        """Return paginated applications, optionally filtered by job opening."""

        effective_limit = limit or self._config.default_list_limit
        if offset < 0:
            raise ApplicationValidationError("offset must be >= 0")
        if effective_limit <= 0:
            raise ApplicationValidationError("limit must be >= 1")
        if effective_limit > self._config.max_list_limit:
            raise ApplicationValidationError(
                f"limit cannot be greater than {self._config.max_list_limit}"
            )
        if submitted_from and submitted_to and submitted_to < submitted_from:
            raise ApplicationValidationError("submitted_to must be later than submitted_from")

        min_total_years_experience: float | None = None
        max_total_years_experience: float | None = None
        effective_keyword_search = keyword_search

        if prefilter_by_job_opening:
            if job_opening_id is None:
                raise ApplicationValidationError(
                    "job_opening_id is required when prefilter_by_job_opening=true"
                )
            opening = await self._job_opening_repository.get(job_opening_id)
            if opening is None:
                raise ApplicationValidationError("job opening not found for prefilter")

            if not effective_keyword_search:
                effective_keyword_search = self._build_keyword_query_from_opening(opening)

            min_total_years_experience, max_total_years_experience = self._parse_experience_range(
                opening.experience_range
            )
            if experience_within_range is None:
                experience_within_range = True

        items, total = await self._repository.list(
            offset=offset,
            limit=effective_limit,
            job_opening_id=job_opening_id,
            role_selection=role_selection,
            applicant_status=applicant_status,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            keyword_search=effective_keyword_search,
            min_total_years_experience=min_total_years_experience,
            max_total_years_experience=max_total_years_experience,
            experience_within_range=experience_within_range,
        )
        return ApplicationListResponse(
            items=items,
            total=total,
            offset=offset,
            limit=effective_limit,
        )

    async def get_by_id(self, application_id: UUID) -> ApplicationRecord | None:
        """Return one application record by UUID."""

        return await self._repository.get_by_id(application_id)

    async def update_applicant_status(
        self,
        *,
        application_id: UUID,
        applicant_status: ApplicantStatus,
        note: str | None = None,
    ) -> ApplicationRecord | None:
        """Update applicant status and return the updated record."""

        updated = await self._repository.update_applicant_status(
            application_id=application_id,
            applicant_status=applicant_status,
            note=note,
        )
        if not updated:
            return None
        return await self._repository.get_by_id(application_id)

    async def update_admin_review(
        self,
        *,
        application_id: UUID,
        updates: dict[str, object],
    ) -> ApplicationRecord | None:
        """Update admin review fields and return updated candidate record."""

        ai_score = updates.get("ai_score")
        if isinstance(ai_score, (int, float)):
            if float(ai_score) < float(self._config.ai_score_threshold):
                updates.setdefault("applicant_status", "rejected")
                updates.setdefault("rejection_reason", self._config.ai_score_fail_reason)
            else:
                updates.setdefault("applicant_status", "shortlisted")
                is_not_rejected = updates.get("applicant_status") != "rejected"
                if is_not_rejected and "rejection_reason" not in updates:
                    updates["rejection_reason"] = None

        updated = await self._repository.update_admin_review(
            application_id=application_id,
            updates=updates,
        )
        if not updated:
            return None
        return await self._repository.get_by_id(application_id)

    def _normalize_payload(self, payload: ApplicationCreatePayload) -> ApplicationCreatePayload:
        """Trim user text fields."""

        return ApplicationCreatePayload(
            full_name=payload.full_name.strip(),
            email=payload.email,
            linkedin_url=payload.linkedin_url,
            portfolio_url=payload.portfolio_url,
            github_url=payload.github_url,
            twitter_url=payload.twitter_url,
            role_selection=payload.role_selection.strip(),
        )

    def _validate_resume_type(self, *, extension: str, content_type: str) -> None:
        """Validate upload extension and MIME type."""

        if extension not in self._config.allowed_resume_extensions:
            raise ApplicationValidationError(self._config.invalid_resume_format_message)
        if content_type not in self._config.allowed_resume_content_types:
            raise ApplicationValidationError(self._config.invalid_resume_format_message)

    def _resolve_max_size_mb(self, extension: str) -> int:
        """Return file-size limit (MB) for the given resume extension."""

        if extension == ".pdf":
            return self._config.max_pdf_size_mb
        if extension == ".doc":
            return self._config.max_doc_size_mb
        if extension == ".docx":
            return self._config.max_docx_size_mb
        raise ApplicationValidationError(f"unsupported resume extension: {extension}")

    def _build_keyword_query_from_opening(self, opening: JobOpeningRecord) -> str:
        """Build simple prefilter keyword query derived from one job opening."""

        stop_words = {item.casefold() for item in self._config.prefilter_stop_words}
        source_text = " ".join(
            [opening.role_title, *opening.requirements, *opening.responsibilities]
        )
        tokens = re.findall(r"[A-Za-z0-9\+\#\.]{2,}", source_text.casefold())
        keywords: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if len(token) < self._config.prefilter_min_keyword_length:
                continue
            if token.isdigit():
                continue
            if token in stop_words:
                continue
            if token in seen:
                continue
            seen.add(token)
            keywords.append(token)
            if len(keywords) >= self._config.prefilter_max_keywords:
                break
        return " ".join(keywords)

    @staticmethod
    def _parse_experience_range(experience_range: str) -> tuple[float | None, float | None]:
        """Parse experience range string like '2-4 years' into numeric bounds."""

        years = re.findall(r"\d+", experience_range)
        if len(years) < 2:
            return None, None
        lower = float(years[0])
        upper = float(years[1])
        if lower > upper:
            return upper, lower
        return lower, upper
