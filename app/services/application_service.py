"""Business logic for candidate application submissions."""

from __future__ import annotations

import logging
import json
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlparse
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
from app.infra.s3_store import S3ObjectStore
from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
)
from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.application import (
    ApplicantStatus,
    ApplicationCreatePayload,
    ApplicationRecord,
    ManagerSelectionDetails,
    ResumeFileMeta,
    StatusHistoryEntry,
)
from app.schemas.application import ApplicationListResponse
from app.schemas.job_opening import JobOpeningRecord
from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSendError,
    EmailSender,
    ManagerDecisionRejectionEmail,
    OfferLetterSignedAlertEmail,
    OfferLetterCandidateEmail,
    SlackJoinManagerAlertEmail,
    SlackWorkspaceInviteEmail,
)
from app.services.parse_queue import ParseQueuePublishError, ParseQueuePublisher, ResumeParseJob
from app.services.resume_storage import ResumeStorage

if TYPE_CHECKING:
    from app.services.docusign_service import DocusignService
    from app.services.offer_letter_service import OfferLetterService
    from app.services.slack_service import SlackService
    from app.services.slack_welcome_service import SlackWelcomeService

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
        offer_letter_service: OfferLetterService | None = None,
        docusign_service: DocusignService | None = None,
        slack_service: SlackService | None = None,
        slack_welcome_service: SlackWelcomeService | None = None,
        s3_store: S3ObjectStore | None = None,
        s3_bucket: str | None = None,
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
        self._offer_letter_service = offer_letter_service
        self._docusign_service = docusign_service
        self._slack_service = slack_service
        self._slack_welcome_service = slack_welcome_service
        self._s3_store = s3_store
        self._s3_bucket = s3_bucket

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
            interview_transcript_status=None,
            interview_transcript_url=None,
            interview_transcript_summary=None,
            interview_transcript_synced_at=None,
            manager_decision=None,
            manager_decision_at=None,
            manager_decision_note=None,
            manager_selection_details=None,
            manager_selection_template_output=None,
            offer_letter_status=None,
            offer_letter_storage_path=None,
            offer_letter_signed_storage_path=None,
            offer_letter_generated_at=None,
            offer_letter_sent_at=None,
            offer_letter_signed_at=None,
            offer_letter_error=None,
            docusign_envelope_id=None,
            slack_invite_status=None,
            slack_invited_at=None,
            slack_user_id=None,
            slack_joined_at=None,
            slack_welcome_message=None,
            slack_welcome_sent_at=None,
            slack_onboarding_status=None,
            slack_error=None,
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
        roles: list[str] = []
        for item in openings:
            if item.paused:
                continue

            open_at = item.application_open_at
            close_at = item.application_close_at
            if open_at.tzinfo is None:
                open_at = open_at.replace(tzinfo=timezone.utc)
            else:
                open_at = open_at.astimezone(timezone.utc)
            if close_at.tzinfo is None:
                close_at = close_at.replace(tzinfo=timezone.utc)
            else:
                close_at = close_at.astimezone(timezone.utc)

            if open_at <= now <= close_at:
                roles.append(item.role_title)
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

    async def record_manager_decision(
        self,
        *,
        application_id: UUID,
        decision: Literal["select", "reject"],
        note: str | None = None,
        selection_details: ManagerSelectionDetails | None = None,
    ) -> ApplicationRecord | None:
        """Record manager select/reject decision after interview completion."""

        candidate = await self._repository.get_by_id(application_id)
        if candidate is None:
            return None
        if candidate.interview_schedule_status != "interview_done":
            raise ApplicationValidationError(
                "manager decision is allowed only when interview_schedule_status is interview_done"
            )

        decision_note = (note or "").strip() or None
        decision_time = datetime.now(tz=timezone.utc)
        updates: dict[str, object] = {
            "manager_decision": decision,
            "manager_decision_at": decision_time,
            "manager_decision_note": decision_note,
            "note": decision_note,
        }

        if decision == "select":
            if selection_details is None:
                raise ApplicationValidationError("selection_details are required for decision=select")
            if self._offer_letter_service is not None:
                rendered_template = await self._offer_letter_service.generate_offer_letter(
                    candidate=candidate,
                    selection_details=selection_details,
                )
            else:
                rendered_template = self._render_manager_selection_template(
                    candidate=candidate,
                    selection_details=selection_details,
                )
            pdf_bytes = self._build_offer_letter_pdf(rendered_template)
            storage_path = await self._store_offer_letter_pdf(
                application_id=application_id,
                pdf_bytes=pdf_bytes,
            )

            updates["applicant_status"] = "offer_letter_created"
            updates["rejection_reason"] = None
            updates["manager_selection_details"] = selection_details.model_dump(mode="json")
            updates["manager_selection_template_output"] = rendered_template
            updates["offer_letter_status"] = "created"
            updates["offer_letter_storage_path"] = storage_path
            updates["offer_letter_signed_storage_path"] = None
            updates["offer_letter_generated_at"] = decision_time
            updates["offer_letter_sent_at"] = None
            updates["offer_letter_signed_at"] = None
            updates["offer_letter_error"] = None
            updates["docusign_envelope_id"] = None
        else:
            updates["applicant_status"] = "rejected"
            updates["rejection_reason"] = decision_note or "Rejected by manager after interview"
            updates["manager_selection_details"] = None
            updates["manager_selection_template_output"] = None
            updates["offer_letter_status"] = "rejected"
            updates["offer_letter_storage_path"] = None
            updates["offer_letter_signed_storage_path"] = None
            updates["offer_letter_sent_at"] = None
            updates["offer_letter_signed_at"] = None
            updates["docusign_envelope_id"] = None

        updated = await self._repository.update_admin_review(
            application_id=application_id,
            updates=updates,
        )
        if not updated:
            return None
        refreshed = await self._repository.get_by_id(application_id)
        if refreshed is None:
            return None
        if decision == "reject":
            await self._send_manager_rejection_email(candidate=refreshed)
        return refreshed

    async def approve_offer_letter(
        self,
        *,
        application_id: UUID,
    ) -> ApplicationRecord | None:
        """Send generated offer letter to candidate after manager approval."""

        candidate = await self._repository.get_by_id(application_id)
        if candidate is None:
            return None
        if candidate.manager_decision != "select":
            raise ApplicationValidationError("candidate is not selected by manager")
        if candidate.offer_letter_status not in {"created", "sent"}:
            raise ApplicationValidationError("offer letter is not in a sendable state")
        if candidate.offer_letter_status == "sent" and candidate.docusign_envelope_id:
            raise ApplicationValidationError("offer letter is already sent for eSignature")
        if not candidate.offer_letter_storage_path:
            raise ApplicationValidationError("offer letter file is missing")

        pdf_bytes = await self._read_offer_letter_pdf_from_storage(candidate.offer_letter_storage_path)
        now = datetime.now(tz=timezone.utc)

        if self._docusign_service is not None and self._docusign_service.enabled:
            try:
                dispatch = await self._docusign_service.send_offer_for_signature(
                    application_id=application_id,
                    candidate_name=candidate.full_name,
                    candidate_email=str(candidate.email),
                    role_title=candidate.role_selection,
                    pdf_bytes=pdf_bytes,
                )
            except Exception as exc:
                raise ApplicationValidationError(
                    f"failed to send offer letter via docusign: {exc}"
                ) from exc
            updates = {
                "applicant_status": "offer_letter_sent",
                "offer_letter_status": "sent_for_signature",
                "offer_letter_sent_at": now,
                "offer_letter_error": None,
                "docusign_envelope_id": dispatch.envelope_id,
                "note": "offer letter approved by manager and sent via DocuSign",
            }
        else:
            await self._send_offer_letter_email(candidate=candidate, pdf_bytes=pdf_bytes)
            updates = {
                "applicant_status": "offer_letter_sent",
                "offer_letter_status": "sent",
                "offer_letter_sent_at": now,
                "offer_letter_error": None,
                "note": "offer letter approved by manager and sent to candidate",
            }

        updated = await self._repository.update_admin_review(
            application_id=application_id,
            updates=updates,
        )
        if not updated:
            return None
        return await self._repository.get_by_id(application_id)

    async def handle_docusign_webhook(
        self,
        *,
        application_id: UUID,
        webhook_token: str | None,
        raw_body: bytes,
        content_type: str | None,
    ) -> bool:
        """Process one DocuSign webhook event and update offer signature status."""

        if self._docusign_service is None or not self._docusign_service.enabled:
            raise ApplicationValidationError("DocuSign is not configured")
        try:
            self._docusign_service.validate_webhook_secret(token=webhook_token)
            event = self._docusign_service.parse_webhook_event(
                raw_body=raw_body,
                content_type=content_type,
            )
        except Exception as exc:
            raise ApplicationValidationError(str(exc)) from exc

        candidate = await self._repository.get_by_id(application_id)
        if candidate is None:
            return False
        if (
            candidate.docusign_envelope_id
            and event.envelope_id
            and candidate.docusign_envelope_id.strip() != event.envelope_id.strip()
        ):
            raise ApplicationValidationError("DocuSign envelope id does not match this candidate")

        updates: dict[str, object] = {}
        transition_to_signed = False
        if event.envelope_id and not candidate.docusign_envelope_id:
            updates["docusign_envelope_id"] = event.envelope_id

        if event.status == "completed":
            should_capture_signed_offer = (
                candidate.offer_letter_status != "signed"
                or not candidate.offer_letter_signed_storage_path
            )
            if should_capture_signed_offer:
                try:
                    updates.update(
                        await self._build_signed_offer_storage_updates(
                            candidate=candidate,
                            envelope_id=event.envelope_id,
                        )
                    )
                except ApplicationValidationError as exc:
                    updates["offer_letter_error"] = self._truncate_error(str(exc))
            if candidate.offer_letter_status != "signed":
                updates["offer_letter_status"] = "signed"
                updates["offer_letter_signed_at"] = datetime.now(tz=timezone.utc)
                updates["applicant_status"] = "offer_letter_sign"
                if "offer_letter_error" not in updates:
                    updates["offer_letter_error"] = None
                updates["note"] = "candidate signed offer letter in DocuSign"
                transition_to_signed = True
        elif event.status in {"declined", "voided"}:
            updates["offer_letter_status"] = event.status
            updates["offer_letter_error"] = f"offer letter {event.status} in DocuSign"
            updates["note"] = f"DocuSign update: {event.status}"

        if not updates:
            return True

        updated = await self._repository.update_admin_review(
            application_id=application_id,
            updates=updates,
        )
        if not updated:
            return False

        refreshed = await self._repository.get_by_id(application_id)
        if refreshed is None:
            return False
        if transition_to_signed:
            await self._send_offer_signed_alert(candidate=refreshed)
        if refreshed.offer_letter_status == "signed" and (
            transition_to_signed or self._needs_slack_invite_retry(refreshed)
        ):
            await self._trigger_slack_invite_after_signature(candidate=refreshed)
        return True

    async def sync_offer_letter_signature_status(
        self,
        *,
        application_id: UUID,
    ) -> ApplicationRecord | None:
        """Sync envelope status from DocuSign and persist signed/declined states."""

        candidate = await self._repository.get_by_id(application_id)
        if candidate is None:
            return None
        if self._docusign_service is None or not self._docusign_service.enabled:
            raise ApplicationValidationError("DocuSign is not configured")
        if not candidate.docusign_envelope_id:
            raise ApplicationValidationError("DocuSign envelope id is missing for this candidate")

        try:
            envelope = await self._docusign_service.get_envelope_status(
                envelope_id=candidate.docusign_envelope_id,
            )
        except Exception as exc:
            raise ApplicationValidationError(
                f"failed to fetch DocuSign envelope status: {exc}"
            ) from exc

        updates: dict[str, object] = {}
        transition_to_signed = False

        if envelope.envelope_id and candidate.docusign_envelope_id != envelope.envelope_id:
            updates["docusign_envelope_id"] = envelope.envelope_id

        if envelope.status == "completed":
            should_capture_signed_offer = (
                candidate.offer_letter_status != "signed"
                or not candidate.offer_letter_signed_storage_path
            )
            if should_capture_signed_offer:
                try:
                    updates.update(
                        await self._build_signed_offer_storage_updates(
                            candidate=candidate,
                            envelope_id=envelope.envelope_id,
                        )
                    )
                except ApplicationValidationError as exc:
                    updates["offer_letter_error"] = self._truncate_error(str(exc))
            if candidate.offer_letter_status != "signed":
                updates["offer_letter_status"] = "signed"
                updates["offer_letter_signed_at"] = datetime.now(tz=timezone.utc)
                updates["applicant_status"] = "offer_letter_sign"
                if "offer_letter_error" not in updates:
                    updates["offer_letter_error"] = None
                updates["note"] = "candidate signed offer letter in DocuSign"
                transition_to_signed = True
        elif envelope.status in {"declined", "voided"}:
            if candidate.offer_letter_status != envelope.status:
                updates["offer_letter_status"] = envelope.status
                updates["offer_letter_error"] = f"offer letter {envelope.status} in DocuSign"
                updates["note"] = f"DocuSign update: {envelope.status}"
        elif envelope.status in {"sent", "delivered"}:
            if candidate.offer_letter_status != "sent_for_signature":
                updates["offer_letter_status"] = "sent_for_signature"
        elif envelope.status != "unknown":
            if candidate.offer_letter_status != envelope.status:
                updates["offer_letter_status"] = envelope.status
                updates["note"] = f"DocuSign status sync: {envelope.status}"

        if updates:
            updated = await self._repository.update_admin_review(
                application_id=application_id,
                updates=updates,
            )
            if not updated:
                return None

        refreshed = await self._repository.get_by_id(application_id)
        if refreshed is None:
            return None
        if transition_to_signed:
            await self._send_offer_signed_alert(candidate=refreshed)
        if refreshed.offer_letter_status == "signed" and (
            transition_to_signed or self._needs_slack_invite_retry(refreshed)
        ):
            await self._trigger_slack_invite_after_signature(candidate=refreshed)
            latest = await self._repository.get_by_id(application_id)
            if latest is not None:
                return latest
        return refreshed

    async def retry_slack_invite(
        self,
        *,
        application_id: UUID,
    ) -> ApplicationRecord | None:
        """Retry Slack invite/onboarding kickoff for one signed candidate."""

        candidate = await self._repository.get_by_id(application_id)
        if candidate is None:
            return None
        if candidate.offer_letter_status != "signed":
            raise ApplicationValidationError(
                "Slack invite can be retried only after offer letter is signed"
            )
        await self._trigger_slack_invite_after_signature(candidate=candidate)
        return await self._repository.get_by_id(application_id)

    async def handle_slack_webhook(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> dict[str, object]:
        """Process one Slack events callback request."""
        payload: dict[str, object]
        try:
            decoded = json.loads(raw_body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ApplicationValidationError("invalid Slack event payload") from exc
        if not isinstance(decoded, dict):
            raise ApplicationValidationError("invalid Slack event payload")
        payload = decoded

        payload_type = str(payload.get("type") or "").strip()
        if payload_type == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not challenge.strip():
                raise ApplicationValidationError("Slack URL verification challenge is missing")
            return {"challenge": challenge}

        if self._slack_service is None or not self._slack_service.enabled:
            raise ApplicationValidationError("Slack onboarding is not configured")

        normalized_headers = {key.casefold(): value for key, value in headers.items()}
        try:
            self._slack_service.validate_event_signature(
                headers=normalized_headers,
                raw_body=raw_body,
            )
        except Exception as exc:
            raise ApplicationValidationError(str(exc)) from exc

        if payload_type != "event_callback":
            return {"processed": False}

        event = payload.get("event")
        if not isinstance(event, dict):
            return {"processed": False}
        if str(event.get("type") or "").strip() != "team_join":
            return {"processed": False}

        user = event.get("user")
        if not isinstance(user, dict):
            return {"processed": False}
        user_id = str(user.get("id") or "").strip()
        profile = user.get("profile")
        email = ""
        if isinstance(profile, dict):
            email = str(profile.get("email") or "").strip()
        if not email:
            email = str(user.get("email") or "").strip()
        if not user_id or not email:
            return {"processed": False}

        processed = await self._process_slack_team_join(
            slack_user_id=user_id,
            candidate_email=email,
        )
        return {"processed": processed}

    async def _trigger_slack_invite_after_signature(self, *, candidate: ApplicationRecord) -> None:
        """Kick off Slack invite immediately after offer signature."""

        if self._slack_service is None or not self._slack_service.enabled:
            return

        now = datetime.now(tz=timezone.utc)
        try:
            selection = candidate.manager_selection_details
            role_title = (
                selection.confirmed_job_title
                if selection is not None
                else candidate.role_selection
            )
            invite = await self._slack_service.invite_candidate(
                candidate_email=str(candidate.email),
                candidate_name=candidate.full_name,
                role_title=role_title,
            )
            updates: dict[str, object] = {
                "slack_invite_status": invite.status,
                "slack_invited_at": now,
                "slack_error": None,
            }
            if invite.user_id:
                updates["slack_user_id"] = invite.user_id
            if invite.status in {"already_in_workspace", "already_in_team"}:
                updates["slack_onboarding_status"] = "onboarded"
                updates["slack_joined_at"] = now
                updates["note"] = "candidate already in Slack workspace after offer signature"
            elif invite.status in {"invited", "already_invited"}:
                updates["slack_onboarding_status"] = "invited"
                updates["note"] = "Slack invite sent after offer signature"
            await self._repository.update_admin_review(
                application_id=candidate.id,
                updates=updates,
            )
            invite_link = self._config.slack_invite_fallback_join_url.strip() or "https://app.slack.com/client"
            await self._send_slack_invite_link_email(
                candidate=candidate,
                invite_link=invite_link,
            )
        except Exception as exc:
            fallback_link = self._config.slack_invite_fallback_join_url.strip()
            if fallback_link:
                sent_fallback = await self._send_slack_invite_link_email(
                    candidate=candidate,
                    invite_link=fallback_link,
                )
                if sent_fallback:
                    await self._repository.update_admin_review(
                        application_id=candidate.id,
                        updates={
                            "slack_invite_status": "invite_link_sent",
                            "slack_invited_at": now,
                            "slack_onboarding_status": "invited",
                            # Fallback path is successful; keep status clean for admins.
                            "slack_error": None,
                            "note": "Slack API invite failed; fallback invite link email sent",
                        },
                    )
                    return
            raw_error = self._truncate_error(str(exc))
            lowered = raw_error.casefold()
            if (
                "not_allowed_token_type" in lowered
                or "admin invite token is missing" in lowered
                or "missing slack token" in lowered
            ):
                invite_status = "action_required"
                invite_note = "Slack invite requires admin token configuration"
            else:
                invite_status = "failed"
                invite_note = "Slack invite failed after offer signature"
            await self._repository.update_admin_review(
                application_id=candidate.id,
                updates={
                    "slack_invite_status": invite_status,
                    "slack_onboarding_status": "invite_action_required"
                    if invite_status == "action_required"
                    else "failed",
                    "slack_error": raw_error,
                    "note": invite_note,
                },
            )

    @staticmethod
    def _needs_slack_invite_retry(candidate: ApplicationRecord) -> bool:
        """Return True when signed candidate still needs Slack invite/onboarding kickoff."""

        status = (candidate.slack_invite_status or "").strip().casefold()
        if not status:
            return True
        return status in {"failed", "action_required", "invite_link_sent"}

    async def _process_slack_team_join(
        self,
        *,
        slack_user_id: str,
        candidate_email: str,
    ) -> bool:
        """Handle Slack first-join event, send AI welcome and notify HR."""

        if self._slack_service is None or not self._slack_service.enabled:
            return False
        candidate = await self._repository.get_latest_by_email(email=candidate_email)
        if candidate is None:
            return False

        now = datetime.now(tz=timezone.utc)
        base_updates: dict[str, object] = {
            "slack_user_id": slack_user_id,
            "slack_joined_at": now,
        }
        if candidate.offer_letter_status != "signed":
            await self._repository.update_admin_review(
                application_id=candidate.id,
                updates={
                    **base_updates,
                    "slack_onboarding_status": "joined_pending_signature",
                    "slack_error": "candidate joined Slack before offer signature completion",
                },
            )
            return False

        if (
            candidate.slack_welcome_sent_at is not None
            and candidate.slack_user_id
            and candidate.slack_user_id.strip() == slack_user_id
        ):
            return True

        opening = await self._job_opening_repository.get(candidate.job_opening_id)
        manager_name = self._extract_manager_display_name(opening.manager_email if opening else "")
        onboarding_links = (
            list(self._slack_service.onboarding_resource_links)
            if self._slack_service is not None
            else []
        )

        try:
            if self._slack_welcome_service is None:
                raise ApplicationValidationError("Slack welcome generator is not configured")
            welcome_message = await self._slack_welcome_service.generate_welcome_message(
                candidate=candidate,
                manager_name=manager_name,
                onboarding_links=onboarding_links,
            )
            await self._slack_service.send_direct_message(
                user_id=slack_user_id,
                text=welcome_message,
            )
            selection = candidate.manager_selection_details
            role_title = (
                selection.confirmed_job_title
                if selection is not None
                else candidate.role_selection
            )
            start_date = selection.start_date.isoformat() if selection is not None else "Not specified"
            await self._slack_service.notify_hr_channel(
                text=(
                    "Onboarding complete: "
                    f"{candidate.full_name} ({candidate.email}) joined Slack. "
                    f"Role: {role_title}. Start date: {start_date}. "
                    f"Manager: {manager_name or 'Not specified'}."
                )
            )
            await self._send_manager_slack_join_alert(
                candidate=candidate,
                opening=opening,
                joined_at=now,
            )

            updated = await self._repository.update_admin_review(
                application_id=candidate.id,
                updates={
                    **base_updates,
                    "slack_welcome_message": welcome_message,
                    "slack_welcome_sent_at": now,
                    "slack_onboarding_status": "onboarded",
                    "slack_error": None,
                    "note": "candidate joined Slack and onboarding welcome was delivered",
                },
            )
            return bool(updated)
        except Exception as exc:
            await self._repository.update_admin_review(
                application_id=candidate.id,
                updates={
                    **base_updates,
                    "slack_onboarding_status": "failed",
                    "slack_error": self._truncate_error(str(exc)),
                    "note": "Slack onboarding processing failed",
                },
            )
            return False

    def _render_manager_selection_template(
        self,
        *,
        candidate: ApplicationRecord,
        selection_details: ManagerSelectionDetails,
    ) -> str:
        """Render configured manager-selection template with submitted details."""

        template = self._config.manager_selection_template
        if not template.strip():
            raise ApplicationValidationError("manager_selection_template is empty")
        values = {
            "candidate_name": candidate.full_name,
            "candidate_email": str(candidate.email),
            "role_applied": candidate.role_selection,
            "confirmed_job_title": selection_details.confirmed_job_title,
            "start_date": selection_details.start_date.isoformat(),
            "base_salary": selection_details.base_salary,
            "compensation_structure": selection_details.compensation_structure,
            "equity_or_bonus": (selection_details.equity_or_bonus or "-"),
            "reporting_manager": selection_details.reporting_manager,
            "custom_terms": (selection_details.custom_terms or "-"),
        }
        try:
            return template.format(**values).strip()
        except KeyError as exc:
            raise ApplicationValidationError(
                f"manager_selection_template is missing placeholder: {exc}"
            ) from exc

    async def _send_offer_letter_email(
        self,
        *,
        candidate: ApplicationRecord,
        pdf_bytes: bytes,
    ) -> None:
        """Send congratulation email with offer-letter PDF attachment."""

        if not self._notification_config.enabled:
            raise ApplicationValidationError("notification is disabled; cannot send offer letter")

        filename = f"offer-letter-{candidate.id}.pdf"
        payload = OfferLetterCandidateEmail(
            candidate_name=candidate.full_name,
            candidate_email=str(candidate.email),
            role_title=candidate.role_selection,
            attachment_filename=filename,
            offer_letter_pdf_bytes=pdf_bytes,
        )
        try:
            with anyio.fail_after(self._notification_config.send_timeout_seconds):
                await self._email_sender.send_offer_letter_to_candidate(payload)
        except (EmailSendError, TimeoutError) as exc:
            logger.exception("failed to send offer letter email", extra={"application_id": str(candidate.id)})
            raise ApplicationValidationError("failed to send offer letter email") from exc

    async def _send_manager_rejection_email(self, *, candidate: ApplicationRecord) -> None:
        """Send manager-final rejection notice to candidate."""

        if not self._notification_config.enabled:
            return

        payload = ManagerDecisionRejectionEmail(
            candidate_name=candidate.full_name,
            candidate_email=str(candidate.email),
            role_title=candidate.role_selection,
        )
        try:
            with anyio.fail_after(self._notification_config.send_timeout_seconds):
                await self._email_sender.send_manager_rejection_notice(payload)
        except (EmailSendError, TimeoutError):
            logger.exception(
                "failed to send manager rejection email",
                extra={"application_id": str(candidate.id)},
            )
            if self._notification_config.fail_submission_on_send_error:
                raise ApplicationValidationError("failed to send rejection email") from None

    async def _send_offer_signed_alert(self, *, candidate: ApplicationRecord) -> None:
        """Alert hiring manager as soon as candidate signs the offer."""

        if not self._notification_config.enabled:
            return

        opening = await self._job_opening_repository.get(candidate.job_opening_id)
        if opening is None:
            return

        manager_email = (opening.manager_email or "").strip()
        if not manager_email:
            return

        payload = OfferLetterSignedAlertEmail(
            manager_email=manager_email,
            manager_name=manager_email,
            candidate_name=candidate.full_name,
            candidate_email=str(candidate.email),
            role_title=candidate.role_selection,
        )
        try:
            with anyio.fail_after(self._notification_config.send_timeout_seconds):
                await self._email_sender.send_offer_letter_signed_alert(payload)
        except (EmailSendError, TimeoutError):
            logger.exception(
                "failed to send signed-offer alert email",
                extra={"application_id": str(candidate.id)},
            )
            if self._notification_config.fail_submission_on_send_error:
                raise ApplicationValidationError("failed to send signed-offer alert email") from None

    async def _send_slack_invite_link_email(
        self,
        *,
        candidate: ApplicationRecord,
        invite_link: str,
    ) -> bool:
        """Send Slack workspace onboarding email to candidate."""

        if not self._notification_config.enabled:
            return False
        payload = SlackWorkspaceInviteEmail(
            candidate_name=candidate.full_name,
            candidate_email=str(candidate.email),
            role_title=(
                candidate.manager_selection_details.confirmed_job_title
                if candidate.manager_selection_details is not None
                else candidate.role_selection
            ),
            slack_invite_link=invite_link,
        )
        try:
            with anyio.fail_after(self._notification_config.send_timeout_seconds):
                await self._email_sender.send_slack_workspace_invite(payload)
            return True
        except (EmailSendError, TimeoutError):
            logger.exception(
                "failed to send fallback Slack invite email",
                extra={"application_id": str(candidate.id)},
            )
            return False

    async def _send_manager_slack_join_alert(
        self,
        *,
        candidate: ApplicationRecord,
        opening: JobOpeningRecord | None,
        joined_at: datetime,
    ) -> None:
        """Notify hiring manager that candidate joined Slack onboarding."""

        if not self._notification_config.enabled:
            return
        if opening is None:
            return

        manager_email = (opening.manager_email or "").strip()
        if not manager_email:
            return

        manager_name = self._extract_manager_display_name(manager_email) or manager_email
        selection = candidate.manager_selection_details
        role_title = (
            selection.confirmed_job_title if selection is not None else candidate.role_selection
        )
        start_date = selection.start_date.isoformat() if selection is not None else "Not specified"
        payload = SlackJoinManagerAlertEmail(
            manager_email=manager_email,
            manager_name=manager_name,
            candidate_name=candidate.full_name,
            candidate_email=str(candidate.email),
            role_title=role_title,
            start_date=start_date,
            slack_joined_at=joined_at.isoformat(),
        )
        try:
            with anyio.fail_after(self._notification_config.send_timeout_seconds):
                await self._email_sender.send_slack_join_manager_alert(payload)
        except (EmailSendError, TimeoutError):
            # Keep onboarding successful even if this manager alert email fails.
            logger.exception(
                "failed to send manager Slack-joined alert email",
                extra={"application_id": str(candidate.id)},
            )

    async def _store_offer_letter_pdf(
        self,
        *,
        application_id: UUID,
        pdf_bytes: bytes,
    ) -> str:
        """Upload generated offer-letter PDF to S3 and return s3:// path."""

        if self._s3_store is None or not self._s3_bucket:
            raise ApplicationValidationError("offer letter S3 storage is not configured")
        prefix = self._config.offer_letter_s3_prefix.strip("/")
        key = f"{prefix}/{application_id}.pdf"
        await self._s3_store.put_bytes(
            key=key,
            payload=pdf_bytes,
            content_type="application/pdf",
        )
        return f"s3://{self._s3_bucket}/{key}"

    async def _build_signed_offer_storage_updates(
        self,
        *,
        candidate: ApplicationRecord,
        envelope_id: str | None,
    ) -> dict[str, object]:
        """Download signed DocuSign PDF and return DB updates for signed storage path."""

        resolved_envelope_id = (envelope_id or candidate.docusign_envelope_id or "").strip()
        if not resolved_envelope_id:
            raise ApplicationValidationError("DocuSign envelope id is missing for signed offer capture")
        if self._docusign_service is None or not self._docusign_service.enabled:
            raise ApplicationValidationError("DocuSign is not configured")
        try:
            signed_document = await self._docusign_service.download_completed_envelope_documents(
                envelope_id=resolved_envelope_id
            )
            signed_storage_path = await self._store_signed_offer_letter_pdf(
                application_id=candidate.id,
                envelope_id=signed_document.envelope_id,
                pdf_bytes=signed_document.pdf_bytes,
            )
        except Exception as exc:
            raise ApplicationValidationError(
                f"failed to capture signed offer letter from DocuSign: {exc}"
            ) from exc
        return {
            "offer_letter_signed_storage_path": signed_storage_path,
            "offer_letter_error": None,
        }

    async def _store_signed_offer_letter_pdf(
        self,
        *,
        application_id: UUID,
        envelope_id: str,
        pdf_bytes: bytes,
    ) -> str:
        """Upload signed offer-letter PDF to S3 and return s3:// path."""

        if self._s3_store is None or not self._s3_bucket:
            raise ApplicationValidationError("offer letter S3 storage is not configured")
        if not pdf_bytes:
            raise ApplicationValidationError("signed offer letter PDF payload is empty")
        prefix = self._config.offer_letter_s3_prefix.strip("/")
        normalized_envelope_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", envelope_id).strip("-")
        if not normalized_envelope_id:
            normalized_envelope_id = "signed"
        key = f"{prefix}/signed/{application_id}-{normalized_envelope_id}.pdf"
        await self._s3_store.put_bytes(
            key=key,
            payload=pdf_bytes,
            content_type="application/pdf",
        )
        return f"s3://{self._s3_bucket}/{key}"

    async def _read_offer_letter_pdf_from_storage(self, storage_path: str) -> bytes:
        """Fetch stored offer-letter PDF bytes from S3 path."""

        if self._s3_store is None:
            raise ApplicationValidationError("offer letter S3 storage is not configured")
        parsed = urlparse(storage_path)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
            raise ApplicationValidationError("invalid offer letter storage path")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        return await self._s3_store.get_bytes(key=key, bucket=bucket)

    def _build_offer_letter_pdf(self, letter_text: str) -> bytes:
        """Convert offer-letter text into a simple multi-page PDF document."""

        normalized = letter_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ApplicationValidationError("offer letter text is empty")

        wrapped_lines: list[str] = []
        for raw in normalized.split("\n"):
            line = raw.strip()
            if not line:
                wrapped_lines.append("")
                continue
            wrapped_lines.extend(textwrap.wrap(line, width=92) or [""])

        lines_per_page = 46
        pages: list[list[str]] = [
            wrapped_lines[index : index + lines_per_page]
            for index in range(0, len(wrapped_lines), lines_per_page)
        ] or [[]]

        def _escape_pdf_text(value: str) -> str:
            ascii_safe = value.encode("latin-1", "replace").decode("latin-1")
            return (
                ascii_safe.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
            )

        objects: dict[int, bytes] = {}
        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"

        page_object_numbers: list[int] = []
        object_index = 4
        for page_lines in pages:
            page_obj = object_index
            content_obj = object_index + 1
            object_index += 2
            page_object_numbers.append(page_obj)

            stream_parts = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"]
            for line in page_lines:
                if line:
                    stream_parts.append(f"({_escape_pdf_text(line)}) Tj")
                stream_parts.append("T*")
            stream_parts.append("ET")
            stream_text = "\n".join(stream_parts).encode("latin-1", "replace")

            objects[page_obj] = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj} 0 R >>"
            ).encode("ascii")
            objects[content_obj] = (
                f"<< /Length {len(stream_text)} >>\nstream\n".encode("ascii")
                + stream_text
                + b"\nendstream"
            )

        kids = " ".join(f"{item} 0 R" for item in page_object_numbers)
        objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_object_numbers)} >>".encode(
            "ascii"
        )
        objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

        sorted_object_numbers = sorted(objects)
        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = {0: 0}
        for number in sorted_object_numbers:
            offsets[number] = len(output)
            output.extend(f"{number} 0 obj\n".encode("ascii"))
            output.extend(objects[number])
            output.extend(b"\nendobj\n")

        startxref = len(output)
        output.extend(f"xref\n0 {max(sorted_object_numbers) + 1}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for number in range(1, max(sorted_object_numbers) + 1):
            offset = offsets.get(number, 0)
            output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.extend(
            (
                f"trailer\n<< /Size {max(sorted_object_numbers) + 1} /Root 1 0 R >>\n"
                f"startxref\n{startxref}\n%%EOF\n"
            ).encode("ascii")
        )
        return bytes(output)

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

    @staticmethod
    def _extract_manager_display_name(manager_email: str | None) -> str:
        """Derive a readable manager display name from manager_email."""

        email = (manager_email or "").strip()
        if not email or "@" not in email:
            return ""
        local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
        if not local:
            return email
        return " ".join(part.capitalize() for part in local.split())

    @staticmethod
    def _truncate_error(message: str, *, limit: int = 900) -> str:
        """Trim long error strings before persisting."""

        cleaned = message.strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit] + "..."
