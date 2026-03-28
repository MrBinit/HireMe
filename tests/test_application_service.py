"""Tests for application submission and duplicate handling behavior."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.datastructures import Headers, UploadFile

from app.core.runtime_config import (
    ApplicationRuntimeConfig,
    JobOpeningRuntimeConfig,
    NotificationRuntimeConfig,
    ParseRuntimeConfig,
    ResearchRuntimeConfig,
    SchedulingRuntimeConfig,
)
from app.repositories.local_application_repository import LocalApplicationRepository
from app.repositories.local_job_opening_repository import LocalJobOpeningRepository
from app.schemas.application import ApplicationCreatePayload
from app.schemas.application import ManagerSelectionDetails
from app.schemas.job_opening import JobOpeningCreatePayload
from app.services.application_service import ApplicationService, ApplicationValidationError
from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSender,
    InitialScreeningRejectionEmail,
    ManagerDecisionRejectionEmail,
    NoopEmailSender,
    OfferLetterSignedAlertEmail,
    OfferLetterCandidateEmail,
    SlackWorkspaceInviteEmail,
)
from app.services.docusign_service import (
    DocusignEnvelopeDispatch,
    DocusignEnvelopeDocument,
    DocusignEnvelopeStatus,
    DocusignWebhookEvent,
)
from app.services.slack_service import SlackInviteResult
from app.services.job_opening_service import JobOpeningService
from app.services.parse_queue import (
    NoopParseQueuePublisher,
    ParseQueuePublishError,
    ParseQueuePublisher,
    ResumeParseJob,
)
from app.services.research_queue import CandidateResearchEnrichmentJob, ResearchQueuePublisher
from app.services.resume_storage import LocalResumeStorage
from app.services.scheduling_queue import CandidateInterviewSchedulingJob, SchedulingQueuePublisher
from app.services.webhook_event_queue import WebhookEventJob, WebhookEventQueuePublisher


class _CaptureParseQueuePublisher(ParseQueuePublisher):
    """Test parse queue publisher that captures enqueued jobs."""

    def __init__(self) -> None:
        self.jobs: list[ResumeParseJob] = []

    async def publish(self, job: ResumeParseJob) -> None:
        self.jobs.append(job)


class _FailingParseQueuePublisher(ParseQueuePublisher):
    """Test parse queue publisher that always fails to publish."""

    async def publish(self, job: ResumeParseJob) -> None:
        _ = job
        raise ParseQueuePublishError("publish failed")


class _CaptureResearchQueuePublisher(ResearchQueuePublisher):
    """Test research queue publisher that captures enqueued jobs."""

    def __init__(self) -> None:
        self.jobs: list[CandidateResearchEnrichmentJob] = []

    async def publish(self, job: CandidateResearchEnrichmentJob) -> None:
        self.jobs.append(job)


class _CaptureSchedulingQueuePublisher(SchedulingQueuePublisher):
    """Test scheduling queue publisher that captures enqueued jobs."""

    def __init__(self) -> None:
        self.jobs: list[CandidateInterviewSchedulingJob] = []

    async def publish(self, job: CandidateInterviewSchedulingJob) -> None:
        self.jobs.append(job)


class _CaptureWebhookEventQueuePublisher(WebhookEventQueuePublisher):
    """Test webhook-event queue publisher that captures enqueued jobs."""

    def __init__(self) -> None:
        self.jobs: list[WebhookEventJob] = []

    async def publish(self, job: WebhookEventJob) -> None:
        self.jobs.append(job)


class _CaptureEmailSender(EmailSender):
    """Test email sender that captures confirmation payloads."""

    def __init__(self) -> None:
        self.payloads = []
        self.initial_rejection_payloads: list[InitialScreeningRejectionEmail] = []
        self.offer_payloads: list[OfferLetterCandidateEmail] = []
        self.manager_rejection_payloads: list[ManagerDecisionRejectionEmail] = []
        self.offer_signed_alert_payloads: list[OfferLetterSignedAlertEmail] = []
        self.slack_invite_payloads: list[SlackWorkspaceInviteEmail] = []

    async def send_application_confirmation(
        self,
        payload: ApplicationConfirmationEmail,
    ) -> None:
        self.payloads.append(payload)

    async def send_initial_screening_rejection(
        self,
        payload: InitialScreeningRejectionEmail,
    ) -> None:
        self.payloads.append(payload)
        self.initial_rejection_payloads.append(payload)

    async def send_offer_letter_to_candidate(
        self,
        payload: OfferLetterCandidateEmail,
    ) -> None:
        self.offer_payloads.append(payload)

    async def send_manager_rejection_notice(
        self,
        payload: ManagerDecisionRejectionEmail,
    ) -> None:
        self.manager_rejection_payloads.append(payload)

    async def send_offer_letter_signed_alert(
        self,
        payload: OfferLetterSignedAlertEmail,
    ) -> None:
        self.offer_signed_alert_payloads.append(payload)

    async def send_slack_workspace_invite(
        self,
        payload: SlackWorkspaceInviteEmail,
    ) -> None:
        self.slack_invite_payloads.append(payload)


class _FakeS3Store:
    """Test S3 store that keeps objects in memory."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, *, key: str, payload: bytes, content_type: str) -> None:
        _ = content_type
        self.objects[key] = payload

    async def get_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        _ = bucket
        return self.objects[key]


class _FakeOfferLetterService:
    """Test offer-letter service that captures generation requests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_offer_letter(self, *, candidate, selection_details) -> str:
        self.calls.append((candidate.full_name, selection_details.confirmed_job_title))
        return (
            f"Subject: Offer of Employment - {selection_details.confirmed_job_title}\n\n"
            f"Dear {candidate.full_name},\n\n"
            "This is an AI-generated offer letter for review."
        )


class _FakeDocusignService:
    """Test DocuSign service for envelope send + webhook events."""

    def __init__(self, *, webhook_token: str = "valid-token") -> None:
        self.enabled = True
        self.webhook_token = webhook_token
        self.send_calls: list[tuple[str, str]] = []
        self.download_calls: list[str] = []
        self.status_calls: list[str] = []
        self.next_event = DocusignWebhookEvent(
            envelope_id="env-123",
            status="completed",
            raw={"status": "completed"},
        )
        self.next_status = DocusignEnvelopeStatus(envelope_id="env-123", status="completed")
        self.next_document = DocusignEnvelopeDocument(
            envelope_id="env-123",
            pdf_bytes=b"%PDF-1.4\n% signed offer letter\n",
        )

    async def send_offer_for_signature(
        self,
        *,
        application_id,
        candidate_name,
        candidate_email,
        role_title,
        pdf_bytes,
    ) -> DocusignEnvelopeDispatch:
        _ = (application_id, role_title, pdf_bytes)
        self.send_calls.append((candidate_name, candidate_email))
        return DocusignEnvelopeDispatch(envelope_id="env-123", status="sent")

    def validate_webhook_secret(
        self,
        *,
        token: str | None,
        raw_body: bytes | None = None,
        signature: str | None = None,
    ) -> None:
        _ = (raw_body, signature)
        if token != self.webhook_token:
            raise ApplicationValidationError("invalid DocuSign webhook token")

    def parse_webhook_event(
        self, *, raw_body: bytes, content_type: str | None
    ) -> DocusignWebhookEvent:
        _ = (raw_body, content_type)
        return self.next_event

    async def get_envelope_status(self, *, envelope_id: str) -> DocusignEnvelopeStatus:
        self.status_calls.append(envelope_id)
        return self.next_status

    async def download_completed_envelope_documents(
        self, *, envelope_id: str
    ) -> DocusignEnvelopeDocument:
        self.download_calls.append(envelope_id)
        return self.next_document


class _FakeSlackService:
    """Test Slack service for invite + webhook + messaging flow."""

    def __init__(
        self,
        *,
        fail_invite: bool = False,
        invite_status: str = "invited",
        invite_user_id: str | None = None,
    ) -> None:
        self.enabled = True
        self.fail_invite = fail_invite
        self.invite_status = invite_status
        self.invite_user_id = invite_user_id
        self.invite_calls: list[tuple[str, str, str]] = []
        self.dm_calls: list[tuple[str, str]] = []
        self.hr_calls: list[str] = []
        self.onboarding_resource_links = [
            "https://intranet.hireme.ai/onboarding",
            "https://intranet.hireme.ai/handbook",
        ]

    def validate_event_signature(self, *, headers, raw_body: bytes) -> None:
        _ = (headers, raw_body)
        return

    def parse_event_payload(self, *, raw_body: bytes) -> dict:
        return json.loads(raw_body.decode("utf-8"))

    async def invite_candidate(
        self,
        *,
        candidate_email: str,
        candidate_name: str,
        role_title: str,
    ) -> SlackInviteResult:
        if self.fail_invite:
            raise ApplicationValidationError("slack invite blocked on this workspace plan")
        self.invite_calls.append((candidate_email, candidate_name, role_title))
        return SlackInviteResult(status=self.invite_status, user_id=self.invite_user_id)

    async def send_direct_message(self, *, user_id: str, text: str) -> None:
        self.dm_calls.append((user_id, text))

    async def notify_hr_channel(self, *, text: str) -> None:
        self.hr_calls.append(text)


class _FakeSlackWelcomeService:
    """Test AI welcome service that returns deterministic personalized text."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_welcome_message(
        self,
        *,
        candidate,
        manager_name: str,
        onboarding_links: list[str],
    ) -> str:
        _ = onboarding_links
        self.calls.append((candidate.full_name, manager_name))
        return (
            f"Welcome {candidate.full_name}! "
            f"Your role is {candidate.role_selection}. "
            f"Your manager {manager_name or 'Hiring Manager'} is excited to have you onboard."
        )


def _build_service(
    tmp_path: Path,
    *,
    use_queue: bool = False,
    parse_queue_publisher: ParseQueuePublisher | None = None,
    research_enrichment_config: ResearchRuntimeConfig.EnrichmentRuntimeConfig | None = None,
    research_queue_publisher: ResearchQueuePublisher | None = None,
    scheduling_config: SchedulingRuntimeConfig | None = None,
    scheduling_queue_publisher: SchedulingQueuePublisher | None = None,
    notification_config: NotificationRuntimeConfig | None = None,
    email_sender: EmailSender | None = None,
    offer_letter_service=None,
    docusign_service=None,
    slack_service=None,
    slack_welcome_service=None,
    webhook_event_queue_publisher: WebhookEventQueuePublisher | None = None,
    s3_store=None,
    slack_invite_fallback_join_url: str | None = None,
    webhook_async_config: ApplicationRuntimeConfig.WebhookAsyncRuntimeConfig | None = None,
) -> tuple[JobOpeningService, ApplicationService]:
    """Create service instances backed by temp local JSON storage."""

    job_repo = LocalJobOpeningRepository(tmp_path / "job_openings.json")
    app_repo = LocalApplicationRepository(tmp_path / "applications.json")

    job_service = JobOpeningService(
        repository=job_repo,
        config=JobOpeningRuntimeConfig(),
    )
    app_service = ApplicationService(
        repository=app_repo,
        job_opening_repository=job_repo,
        config=ApplicationRuntimeConfig(
            slack_invite_fallback_join_url=slack_invite_fallback_join_url or "",
            webhook_async=(
                webhook_async_config
                or ApplicationRuntimeConfig.WebhookAsyncRuntimeConfig(
                    enabled=False,
                    use_queue=False,
                )
            ),
            manager_selection_template=(
                "Subject: Offer of Employment - {confirmed_job_title}\n\n"
                "Dear {candidate_name},\n\n"
                "We are pleased to offer you the position of {confirmed_job_title} at HireMe, "
                "based on your application for {role_applied}. This letter confirms the manager's "
                "selection decision and outlines the key terms of your offer.\n\n"
                "Your anticipated start date is {start_date}, and you will report directly to "
                "{reporting_manager}. Your compensation package includes a base salary of "
                "{base_salary}, with the following compensation structure: {compensation_structure}. "
                "Equity or bonus eligibility for this role is {equity_or_bonus}.\n\n"
                "Additional terms and conditions specific to your offer are as follows: {custom_terms}.\n\n"
                "Please review this offer and confirm acceptance by replying from {candidate_email}. "
                "We look forward to welcoming you to HireMe.\n\n"
                "Sincerely,\n"
                "HireMe Hiring Team\n"
            ),
        ),
        resume_storage=LocalResumeStorage(tmp_path / "resumes"),
        parse_config=ParseRuntimeConfig(use_queue=use_queue),
        parse_queue_publisher=parse_queue_publisher or NoopParseQueuePublisher(),
        research_enrichment_config=research_enrichment_config
        or ResearchRuntimeConfig.EnrichmentRuntimeConfig(use_queue=False),
        research_queue_publisher=research_queue_publisher,
        scheduling_config=scheduling_config
        or SchedulingRuntimeConfig(
            enabled=False,
            use_queue=False,
            auto_enqueue_after_shortlist=False,
        ),
        scheduling_queue_publisher=scheduling_queue_publisher,
        notification_config=notification_config or NotificationRuntimeConfig(enabled=False),
        email_sender=email_sender or NoopEmailSender(),
        offer_letter_service=offer_letter_service,
        docusign_service=docusign_service,
        slack_service=slack_service,
        slack_welcome_service=slack_welcome_service,
        webhook_event_queue_publisher=webhook_event_queue_publisher,
        s3_store=s3_store or _FakeS3Store(),  # type: ignore[arg-type]
        s3_bucket="test-bucket",
    )
    return job_service, app_service


async def _create_opening(job_service: JobOpeningService, *, role_title: str):
    """Create a single opening used by tests."""

    return await job_service.create(
        JobOpeningCreatePayload(
            role_title=role_title,
            manager_email="manager@example.com",
            team="Platform",
            location="remote",
            experience_level="mid",
            experience_range="2-3 years",
            application_open_at=datetime.now(tz=timezone.utc) - timedelta(days=1),
            application_close_at=datetime.now(tz=timezone.utc) + timedelta(days=7),
            responsibilities=["Build APIs"],
            requirements=["Python"],
        )
    )


def _resume_file(name: str = "resume.pdf") -> UploadFile:
    """Return a small in-memory PDF upload."""

    return UploadFile(
        file=io.BytesIO(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"),
        filename=name,
        headers=Headers({"content-type": "application/pdf"}),
    )


def _invalid_resume_file() -> UploadFile:
    """Return an invalid extension/content-type upload."""

    return UploadFile(
        file=io.BytesIO(b"plain text"),
        filename="resume.txt",
        headers=Headers({"content-type": "text/plain"}),
    )


def test_duplicate_email_same_opening_is_blocked(tmp_path: Path) -> None:
    """Same email should not apply twice to the same job opening."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Backend Engineer {uuid4().hex[:6]}",
        )

        payload = ApplicationCreatePayload(
            full_name="Alice",
            email="alice@example.com",
            linkedin_url="https://www.linkedin.com/in/alice",
            portfolio_url="https://alice.dev",
            github_url="https://github.com/alice",
            role_selection=opening.role_title,
        )
        first = await app_service.submit(payload=payload, resume=_resume_file())
        assert first.resume.storage_path.endswith(f"/{first.resume.stored_filename}")

        duplicate_error = None
        try:
            await app_service.submit(payload=payload, resume=_resume_file())
        except ApplicationValidationError as exc:
            duplicate_error = exc

        assert duplicate_error is not None
        assert "duplicate application" in str(duplicate_error)

    asyncio.run(run())


def test_same_email_different_openings_is_allowed(tmp_path: Path) -> None:
    """Same email may apply to different job openings."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening_a = await _create_opening(
            job_service,
            role_title=f"Backend Engineer {uuid4().hex[:6]}",
        )
        opening_b = await _create_opening(
            job_service,
            role_title=f"Data Engineer {uuid4().hex[:6]}",
        )

        await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Bob",
                email="bob@example.com",
                linkedin_url="https://www.linkedin.com/in/bob",
                portfolio_url="https://bob.dev",
                github_url="https://github.com/bob",
                role_selection=opening_a.role_title,
            ),
            resume=_resume_file(),
        )
        second = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Bob",
                email="bob@example.com",
                linkedin_url="https://www.linkedin.com/in/bob",
                portfolio_url="https://bob.dev",
                github_url="https://github.com/bob",
                role_selection=opening_b.role_title,
            ),
            resume=_resume_file(),
        )

        assert second.job_opening_id == opening_b.id
        assert second.role_selection == opening_b.role_title
        assert second.resume.storage_path.endswith(f"/{second.resume.stored_filename}")

    asyncio.run(run())


def test_concurrent_duplicate_submissions_allow_only_one(tmp_path: Path) -> None:
    """Concurrent same-email submissions for one opening should dedupe safely."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Platform Engineer {uuid4().hex[:6]}",
        )

        payload = ApplicationCreatePayload(
            full_name="Concurrent User",
            email="concurrent@example.com",
            linkedin_url="https://www.linkedin.com/in/concurrent",
            portfolio_url="https://concurrent.dev",
            github_url="https://github.com/concurrent",
            role_selection=opening.role_title,
        )

        results = await asyncio.gather(
            app_service.submit(payload=payload, resume=_resume_file()),
            app_service.submit(payload=payload, resume=_resume_file()),
            return_exceptions=True,
        )

        success_count = sum(1 for item in results if not isinstance(item, Exception))
        failure_count = sum(
            1
            for item in results
            if isinstance(item, ApplicationValidationError) and "duplicate application" in str(item)
        )
        assert success_count == 1
        assert failure_count == 1

    asyncio.run(run())


def test_application_rejected_after_close_time(tmp_path: Path) -> None:
    """Application should be rejected when opening window is closed."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await job_service.create(
            JobOpeningCreatePayload(
                role_title=f"Expired Engineer {uuid4().hex[:6]}",
                manager_email="manager@example.com",
                team="Platform",
                location="remote",
                experience_level="mid",
                experience_range="2-3 years",
                application_open_at=datetime.now(tz=timezone.utc) - timedelta(days=5),
                application_close_at=datetime.now(tz=timezone.utc) - timedelta(days=1),
                responsibilities=["Build APIs"],
                requirements=["Python"],
            )
        )

        error = None
        try:
            await app_service.submit(
                payload=ApplicationCreatePayload(
                    full_name="Late User",
                    email="late@example.com",
                    linkedin_url="https://www.linkedin.com/in/late",
                    portfolio_url="https://late.dev",
                    github_url="https://github.com/late",
                    role_selection=opening.role_title,
                ),
                resume=_resume_file(),
            )
        except ApplicationValidationError as exc:
            error = exc

        assert error is not None
        assert "already been closed for this role" in str(error)

    asyncio.run(run())


def test_invalid_resume_format_returns_pdf_docx_guidance(tmp_path: Path) -> None:
    """Invalid resume format should return clear PDF/DOCX guidance."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Format Check Engineer {uuid4().hex[:6]}",
        )

        error = None
        try:
            await app_service.submit(
                payload=ApplicationCreatePayload(
                    full_name="Format User",
                    email="format@example.com",
                    linkedin_url="https://www.linkedin.com/in/format",
                    portfolio_url="https://format.dev",
                    github_url="https://github.com/format",
                    role_selection=opening.role_title,
                ),
                resume=_invalid_resume_file(),
            )
        except ApplicationValidationError as exc:
            error = exc

        assert error is not None
        assert "PDF or DOCX" in str(error)

    asyncio.run(run())


def test_submit_enqueues_parse_job_when_queue_enabled(tmp_path: Path) -> None:
    """Submission should enqueue parse job when parse queue is enabled."""

    async def run() -> None:
        queue = _CaptureParseQueuePublisher()
        job_service, app_service = _build_service(
            tmp_path,
            use_queue=True,
            parse_queue_publisher=queue,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Queue Engineer {uuid4().hex[:6]}",
        )

        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Queue User",
                email="queue@example.com",
                linkedin_url="https://www.linkedin.com/in/queue-user",
                portfolio_url="https://queue.dev",
                github_url="https://github.com/queue-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        assert len(queue.jobs) == 1
        assert queue.jobs[0].application_id == created.id
        assert queue.jobs[0].job_opening_id == created.job_opening_id
        assert queue.jobs[0].resume_storage_path == created.resume.storage_path

    asyncio.run(run())


def test_submit_succeeds_when_queue_publish_fails_by_default(tmp_path: Path) -> None:
    """Submission should succeed even if queue publish fails by default."""

    async def run() -> None:
        job_service, app_service = _build_service(
            tmp_path,
            use_queue=True,
            parse_queue_publisher=_FailingParseQueuePublisher(),
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Failing Queue Engineer {uuid4().hex[:6]}",
        )

        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Queue Fallback",
                email="queue-fallback@example.com",
                linkedin_url="https://www.linkedin.com/in/queue-fallback",
                portfolio_url="https://queue-fallback.dev",
                github_url="https://github.com/queue-fallback",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        assert created.parse_status == "pending"

    asyncio.run(run())


def test_submit_sends_confirmation_email_when_enabled(tmp_path: Path) -> None:
    """Submission should trigger confirmation email when notification is enabled."""

    async def run() -> None:
        email_sender = _CaptureEmailSender()
        job_service, app_service = _build_service(
            tmp_path,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Email Engineer {uuid4().hex[:6]}",
        )

        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Email User",
                email="email@example.com",
                linkedin_url="https://www.linkedin.com/in/email-user",
                portfolio_url="https://email.dev",
                github_url="https://github.com/email-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        assert len(email_sender.payloads) == 1
        assert email_sender.payloads[0].candidate_name == created.full_name
        assert email_sender.payloads[0].candidate_email == str(created.email)

    asyncio.run(run())


def test_admin_can_update_applicant_status(tmp_path: Path) -> None:
    """Applicant status update should persist and return updated record."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Status Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Status User",
                email="status@example.com",
                linkedin_url="https://www.linkedin.com/in/status-user",
                portfolio_url="https://status.dev",
                github_url="https://github.com/status-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        updated = await app_service.update_applicant_status(
            application_id=created.id,
            applicant_status="interview",
        )

        assert updated is not None
        assert updated.id == created.id
        assert updated.applicant_status == "interview"

    asyncio.run(run())


def test_manual_shortlist_enqueues_research_and_scheduling(tmp_path: Path) -> None:
    """Manual move-ahead decision should enqueue same downstream shortlist jobs."""

    async def run() -> None:
        research_queue = _CaptureResearchQueuePublisher()
        scheduling_queue = _CaptureSchedulingQueuePublisher()
        job_service, app_service = _build_service(
            tmp_path,
            research_enrichment_config=ResearchRuntimeConfig.EnrichmentRuntimeConfig(
                use_queue=True,
                provider="sqs",
                queue_url="https://example.com/research-queue",
                target_statuses=["shortlisted"],
            ),
            research_queue_publisher=research_queue,
            scheduling_config=SchedulingRuntimeConfig(
                enabled=True,
                use_queue=True,
                provider="sqs",
                queue_url="https://example.com/scheduling-queue",
                auto_enqueue_after_shortlist=True,
                target_statuses=["shortlisted"],
            ),
            scheduling_queue_publisher=scheduling_queue,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Manual Queue Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Queue Ready User",
                email="queue-ready@example.com",
                linkedin_url="https://www.linkedin.com/in/queue-ready-user",
                portfolio_url="https://queue-ready.dev",
                github_url="https://github.com/queue-ready-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        updated = await app_service.update_applicant_status(
            application_id=created.id,
            applicant_status="shortlisted",
            note="Human review approved. Move ahead.",
        )

        assert updated is not None
        assert updated.applicant_status == "shortlisted"
        assert updated.interview_schedule_status == "queued"
        assert len(research_queue.jobs) == 1
        assert len(scheduling_queue.jobs) == 1
        assert research_queue.jobs[0].application_id == created.id
        assert scheduling_queue.jobs[0].application_id == created.id

    asyncio.run(run())


def test_manual_rejection_sends_initial_screening_rejection_email(tmp_path: Path) -> None:
    """Manual do-not-move-ahead decision should send a rejection email."""

    async def run() -> None:
        email_sender = _CaptureEmailSender()
        job_service, app_service = _build_service(
            tmp_path,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Manual Reject Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Manual Reject User",
                email="manual-reject@example.com",
                linkedin_url="https://www.linkedin.com/in/manual-reject-user",
                portfolio_url="https://manual-reject.dev",
                github_url="https://github.com/manual-reject-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        updated = await app_service.update_applicant_status(
            application_id=created.id,
            applicant_status="rejected",
            note="Not moving ahead after manual review.",
        )

        assert updated is not None
        assert updated.applicant_status == "rejected"
        assert len(email_sender.initial_rejection_payloads) == 1
        payload = email_sender.initial_rejection_payloads[0]
        assert payload.candidate_email == str(created.email)
        assert payload.rejection_reason == "Not moving ahead after manual review."

    asyncio.run(run())


def test_application_rejected_when_job_opening_is_paused(tmp_path: Path) -> None:
    """Submission should be rejected when admin pauses the job opening."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Paused Engineer {uuid4().hex[:6]}",
        )
        paused = await job_service.set_paused(str(opening.id), True)
        assert paused is not None
        assert paused.status == "paused"

        error = None
        try:
            await app_service.submit(
                payload=ApplicationCreatePayload(
                    full_name="Paused User",
                    email="paused@example.com",
                    linkedin_url="https://www.linkedin.com/in/paused-user",
                    portfolio_url="https://paused.dev",
                    github_url="https://github.com/paused-user",
                    role_selection=opening.role_title,
                ),
                resume=_resume_file(),
            )
        except ApplicationValidationError as exc:
            error = exc

        assert error is not None
        assert "paused" in str(error).lower()

    asyncio.run(run())


def test_admin_review_update_persists_ai_fields_and_history(tmp_path: Path) -> None:
    """Admin review update should persist AI metadata and status-history note."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Review Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Review User",
                email="review@example.com",
                linkedin_url="https://www.linkedin.com/in/review-user",
                portfolio_url="https://review.dev",
                github_url="https://github.com/review-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        updated = await app_service.update_admin_review(
            application_id=created.id,
            updates={
                "applicant_status": "shortlisted",
                "note": "Manual override after profile review",
                "ai_score": 88.5,
                "ai_screening_summary": "Strong backend fit.",
                "online_research_summary": "Open-source activity looks relevant.",
            },
        )

        assert updated is not None
        assert updated.applicant_status == "shortlisted"
        assert updated.ai_score == 88.5
        assert updated.ai_screening_summary == "Strong backend fit."
        assert updated.online_research_summary == "Open-source activity looks relevant."
        assert updated.status_history
        assert "Manual override" in (updated.status_history[-1].note or "")

    asyncio.run(run())


def test_admin_review_auto_shortlists_when_ai_score_passes_threshold(tmp_path: Path) -> None:
    """AI score >= threshold should automatically set applicant_status to shortlisted."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Threshold Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Threshold User",
                email="threshold@example.com",
                linkedin_url="https://www.linkedin.com/in/threshold-user",
                portfolio_url="https://threshold.dev",
                github_url="https://github.com/threshold-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        updated = await app_service.update_admin_review(
            application_id=created.id,
            updates={
                "ai_score": 80.0,
                "ai_screening_summary": "Passed threshold.",
            },
        )

        assert updated is not None
        assert updated.applicant_status == "shortlisted"
        assert updated.rejection_reason is None
        assert updated.ai_score == 80.0

    asyncio.run(run())


def test_prefilter_by_job_opening_returns_only_matching_candidates(tmp_path: Path) -> None:
    """Prefilter should keep candidates within experience range and keyword match."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Backend Engineer {uuid4().hex[:6]}",
        )

        matching = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Prefilter Match",
                email="prefilter-match@example.com",
                linkedin_url="https://www.linkedin.com/in/prefilter-match",
                portfolio_url="https://prefilter-match.dev",
                github_url="https://github.com/prefilter-match",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        rejected = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Prefilter Reject",
                email="prefilter-reject@example.com",
                linkedin_url="https://www.linkedin.com/in/prefilter-reject",
                portfolio_url="https://prefilter-reject.dev",
                github_url="https://github.com/prefilter-reject",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        repo = app_service._repository  # noqa: SLF001
        await repo.update_parse_state(
            application_id=matching.id,
            parse_status="completed",
            parse_result={"skills": ["Python", "FastAPI"]},
            parsed_total_years_experience=3.0,
            parsed_search_text="python fastapi postgresql aws",
        )
        await repo.update_parse_state(
            application_id=rejected.id,
            parse_status="completed",
            parse_result={"skills": ["Excel"]},
            parsed_total_years_experience=1.0,
            parsed_search_text="excel sales operations",
        )

        result = await app_service.list(
            offset=0,
            limit=10,
            job_opening_id=opening.id,
            prefilter_by_job_opening=True,
        )

        assert result.total == 1
        assert len(result.items) == 1
        assert str(result.items[0].email) == "prefilter-match@example.com"

    asyncio.run(run())


def test_prefilter_can_show_candidates_outside_experience_range(tmp_path: Path) -> None:
    """Prefilter should support filtering outside job experience range."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Platform Engineer {uuid4().hex[:6]}",
        )

        in_range = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="In Range",
                email="in-range@example.com",
                linkedin_url="https://www.linkedin.com/in/in-range",
                portfolio_url="https://in-range.dev",
                github_url="https://github.com/in-range",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        out_range = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Out Range",
                email="out-range@example.com",
                linkedin_url="https://www.linkedin.com/in/out-range",
                portfolio_url="https://out-range.dev",
                github_url="https://github.com/out-range",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        repo = app_service._repository  # noqa: SLF001
        await repo.update_parse_state(
            application_id=in_range.id,
            parse_status="completed",
            parse_result={"skills": ["Python"]},
            parsed_total_years_experience=3.0,
            parsed_search_text="python fastapi",
        )
        await repo.update_parse_state(
            application_id=out_range.id,
            parse_status="completed",
            parse_result={"skills": ["Python"]},
            parsed_total_years_experience=6.0,
            parsed_search_text="python fastapi",
        )

        result = await app_service.list(
            offset=0,
            limit=10,
            job_opening_id=opening.id,
            prefilter_by_job_opening=True,
            experience_within_range=False,
        )

        assert result.total == 1
        assert len(result.items) == 1
        assert str(result.items[0].email) == "out-range@example.com"

    asyncio.run(run())


def test_manager_select_decision_requires_interview_done_and_creates_offer_letter(
    tmp_path: Path,
) -> None:
    """Manager select should generate PDF + storage path and set created status."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Decision Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Decision User",
                email="decision@example.com",
                linkedin_url="https://www.linkedin.com/in/decision-user",
                portfolio_url="https://decision.dev",
                github_url="https://github.com/decision-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        updated = await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            note="Strong interview performance.",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Senior Backend Engineer",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=14),
                base_salary="USD 150,000",
                compensation_structure="Annual base + 10% performance bonus",
                equity_or_bonus="0.2% stock options",
                reporting_manager="Engineering Director",
                custom_terms="90-day probation with mentorship plan",
            ),
        )

        assert updated is not None
        assert updated.applicant_status == "offer_letter_created"
        assert updated.offer_letter_status == "created"
        assert isinstance(updated.offer_letter_storage_path, str)
        assert updated.offer_letter_storage_path.startswith("s3://test-bucket/")
        assert updated.offer_letter_generated_at is not None
        assert updated.manager_decision == "select"
        assert updated.manager_decision_at is not None
        assert updated.manager_decision_note == "Strong interview performance."
        assert updated.manager_selection_details is not None
        assert updated.manager_selection_details.confirmed_job_title == "Senior Backend Engineer"
        assert isinstance(updated.manager_selection_template_output, str)
        assert "Dear Decision User," in updated.manager_selection_template_output
        assert (
            "position of Senior Backend Engineer at HireMe"
            in updated.manager_selection_template_output
        )
        assert "base salary of USD 150,000" in updated.manager_selection_template_output
        assert updated.rejection_reason is None

    asyncio.run(run())


def test_manager_decision_reject_sets_rejected_status(tmp_path: Path) -> None:
    """Manager reject decision should move candidate to rejected with reason."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Reject Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Reject User",
                email="reject@example.com",
                linkedin_url="https://www.linkedin.com/in/reject-user",
                portfolio_url="https://reject.dev",
                github_url="https://github.com/reject-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        updated = await app_service.record_manager_decision(
            application_id=created.id,
            decision="reject",
            note="Communication was below expected bar.",
        )

        assert updated is not None
        assert updated.applicant_status == "rejected"
        assert updated.manager_decision == "reject"
        assert updated.manager_selection_details is None
        assert updated.rejection_reason == "Communication was below expected bar."

    asyncio.run(run())


def test_manager_reject_sends_candidate_email_when_notifications_enabled(tmp_path: Path) -> None:
    """Manager reject should send final rejection email notice to candidate."""

    async def run() -> None:
        email_sender = _CaptureEmailSender()
        job_service, app_service = _build_service(
            tmp_path,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Reject Mail Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Reject Mail User",
                email="reject-mail@example.com",
                linkedin_url="https://www.linkedin.com/in/reject-mail-user",
                portfolio_url="https://reject-mail.dev",
                github_url="https://github.com/reject-mail-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )

        updated = await app_service.record_manager_decision(
            application_id=created.id,
            decision="reject",
            note="Not the right fit for this role.",
        )

        assert updated is not None
        assert updated.applicant_status == "rejected"
        assert len(email_sender.manager_rejection_payloads) == 1
        assert (
            email_sender.manager_rejection_payloads[0].candidate_email == "reject-mail@example.com"
        )

    asyncio.run(run())


def test_manager_decision_requires_interview_done(tmp_path: Path) -> None:
    """Manager decision should be blocked until interview is marked done."""

    async def run() -> None:
        job_service, app_service = _build_service(tmp_path)
        opening = await _create_opening(
            job_service,
            role_title=f"Guardrail Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Guardrail User",
                email="guardrail@example.com",
                linkedin_url="https://www.linkedin.com/in/guardrail-user",
                portfolio_url="https://guardrail.dev",
                github_url="https://github.com/guardrail-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )

        with pytest.raises(ApplicationValidationError):
            await app_service.record_manager_decision(
                application_id=created.id,
                decision="reject",
                note="Blocked before interview done.",
            )

    asyncio.run(run())


def test_manager_select_uses_ai_offer_letter_service_when_available(tmp_path: Path) -> None:
    """Manager select should store AI-generated letter when generator is configured."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"AI Letter Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="AI Letter User",
                email="ai-letter@example.com",
                linkedin_url="https://www.linkedin.com/in/ai-letter-user",
                portfolio_url="https://ai-letter.dev",
                github_url="https://github.com/ai-letter-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )

        updated = await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Backend Engineer III",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=21),
                base_salary="USD 160,000",
                compensation_structure="Base + bonus",
                equity_or_bonus="0.3% equity",
                reporting_manager="Head of Engineering",
                custom_terms="Standard offer conditions apply",
            ),
        )

        assert updated is not None
        assert len(fake_offer_service.calls) == 1
        assert updated.manager_selection_template_output is not None
        assert "AI-generated offer letter for review" in updated.manager_selection_template_output
        assert "Backend Engineer III" in updated.manager_selection_template_output

    asyncio.run(run())


def test_manager_offer_letter_approval_sends_pdf_and_updates_status(tmp_path: Path) -> None:
    """Approving generated offer letter should email PDF and mark as sent."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        email_sender = _CaptureEmailSender()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Approve Letter Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Approve User",
                email="approve@example.com",
                linkedin_url="https://www.linkedin.com/in/approve-user",
                portfolio_url="https://approve.dev",
                github_url="https://github.com/approve-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        created_offer = await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Backend Engineer IV",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=10),
                base_salary="USD 170,000",
                compensation_structure="Base + annual bonus",
                equity_or_bonus="0.5% equity",
                reporting_manager="Director of Engineering",
                custom_terms="Subject to standard onboarding checks",
            ),
        )

        assert created_offer is not None
        assert created_offer.offer_letter_status == "created"

        sent = await app_service.approve_offer_letter(application_id=created.id)
        assert sent is not None
        assert sent.offer_letter_status == "sent"
        assert sent.applicant_status == "offer_letter_sent"
        assert sent.offer_letter_sent_at is not None
        assert len(email_sender.offer_payloads) == 1
        assert email_sender.offer_payloads[0].candidate_email == "approve@example.com"
        assert email_sender.offer_payloads[0].offer_letter_pdf_bytes.startswith(b"%PDF-1.4")

    asyncio.run(run())


def test_offer_letter_approval_uses_docusign_when_enabled(tmp_path: Path) -> None:
    """Approving offer letter should use DocuSign when integration is configured."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService()
        email_sender = _CaptureEmailSender()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"DocuSign Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="DocuSign User",
                email="docusign@example.com",
                linkedin_url="https://www.linkedin.com/in/docusign-user",
                portfolio_url="https://docusign.dev",
                github_url="https://github.com/docusign-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        created_offer = await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="DocuSign Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=15),
                base_salary="USD 155,000",
                compensation_structure="Base + bonus",
                equity_or_bonus=None,
                reporting_manager="Engineering Manager",
                custom_terms=None,
            ),
        )
        assert created_offer is not None
        assert created_offer.offer_letter_status == "created"

        sent = await app_service.approve_offer_letter(application_id=created.id)
        assert sent is not None
        assert sent.offer_letter_status == "sent_for_signature"
        assert sent.applicant_status == "offer_letter_sent"
        assert sent.docusign_envelope_id == "env-123"
        assert len(fake_docusign.send_calls) == 1
        assert len(email_sender.offer_payloads) == 0

    asyncio.run(run())


def test_docusign_webhook_completed_marks_candidate_signed_and_alerts_manager(
    tmp_path: Path,
) -> None:
    """DocuSign completed callback should mark signed and send manager alert."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService(webhook_token="hook-secret")
        email_sender = _CaptureEmailSender()
        fake_s3 = _FakeS3Store()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
            s3_store=fake_s3,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Signed Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Signed User",
                email="signed@example.com",
                linkedin_url="https://www.linkedin.com/in/signed-user",
                portfolio_url="https://signed.dev",
                github_url="https://github.com/signed-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Signed Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=20),
                base_salary="USD 150,000",
                compensation_structure="Base + bonus",
                equity_or_bonus="0.2% equity",
                reporting_manager="Senior Manager",
                custom_terms=None,
            ),
        )
        sent = await app_service.approve_offer_letter(application_id=created.id)
        assert sent is not None
        assert sent.offer_letter_status == "sent_for_signature"

        processed = await app_service.handle_docusign_webhook(
            application_id=created.id,
            webhook_token="hook-secret",
            raw_body=b'{"event":"envelope-completed"}',
            content_type="application/json",
        )
        assert processed is True

        updated = await app_service.get_by_id(created.id)
        assert updated is not None
        assert updated.offer_letter_status == "signed"
        assert updated.offer_letter_signed_at is not None
        assert isinstance(updated.offer_letter_signed_storage_path, str)
        assert updated.offer_letter_signed_storage_path.startswith("s3://test-bucket/")
        assert "/signed/" in updated.offer_letter_signed_storage_path
        assert updated.applicant_status == "offer_letter_sign"
        assert len(fake_docusign.download_calls) == 1
        assert len([key for key in fake_s3.objects if "/signed/" in key]) == 1
        assert len(email_sender.offer_signed_alert_payloads) == 1
        assert email_sender.offer_signed_alert_payloads[0].manager_email == opening.manager_email

    asyncio.run(run())


def test_sync_signature_status_completed_persists_signed_offer_pdf(tmp_path: Path) -> None:
    """Manual signature sync should store signed offer PDF path when envelope is completed."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService()
        fake_s3 = _FakeS3Store()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            notification_config=NotificationRuntimeConfig(enabled=False),
            s3_store=fake_s3,
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Sync Signature Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Sync Signature User",
                email="sync-signature@example.com",
                linkedin_url="https://www.linkedin.com/in/sync-signature-user",
                portfolio_url="https://sync-signature.dev",
                github_url="https://github.com/sync-signature-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Sync Signature Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=10),
                base_salary="USD 145,000",
                compensation_structure="Base + bonus",
                equity_or_bonus=None,
                reporting_manager="Hiring Manager",
                custom_terms=None,
            ),
        )
        sent = await app_service.approve_offer_letter(application_id=created.id)
        assert sent is not None
        assert sent.offer_letter_status == "sent_for_signature"

        synced = await app_service.sync_offer_letter_signature_status(application_id=created.id)
        assert synced is not None
        assert synced.offer_letter_status == "signed"
        assert synced.offer_letter_signed_at is not None
        assert isinstance(synced.offer_letter_signed_storage_path, str)
        assert synced.offer_letter_signed_storage_path.startswith("s3://test-bucket/")
        assert "/signed/" in synced.offer_letter_signed_storage_path
        assert len(fake_docusign.status_calls) == 1
        assert len(fake_docusign.download_calls) == 1
        assert len([key for key in fake_s3.objects if "/signed/" in key]) == 1

    asyncio.run(run())


def test_docusign_signed_event_triggers_slack_invite(tmp_path: Path) -> None:
    """DocuSign completed callback should trigger Slack invite kickoff."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService(webhook_token="hook-secret")
        fake_slack = _FakeSlackService()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            slack_service=fake_slack,
            notification_config=NotificationRuntimeConfig(enabled=False),
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Slack Invite Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Slack Invite User",
                email="slack-invite@example.com",
                linkedin_url="https://www.linkedin.com/in/slack-invite-user",
                portfolio_url="https://slack-invite.dev",
                github_url="https://github.com/slack-invite-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Slack Invite Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=14),
                base_salary="USD 148,000",
                compensation_structure="Base + bonus",
                equity_or_bonus=None,
                reporting_manager="Engineering Manager",
                custom_terms=None,
            ),
        )
        await app_service.approve_offer_letter(application_id=created.id)

        processed = await app_service.handle_docusign_webhook(
            application_id=created.id,
            webhook_token="hook-secret",
            raw_body=b'{"event":"envelope-completed"}',
            content_type="application/json",
        )
        assert processed is True
        assert len(fake_slack.invite_calls) == 1

        updated = await app_service.get_by_id(created.id)
        assert updated is not None
        assert updated.applicant_status == "offer_letter_sign"
        assert updated.slack_invite_status == "invited"
        assert updated.slack_invited_at is not None

    asyncio.run(run())


def test_docusign_signed_event_marks_onboarded_when_already_in_workspace(tmp_path: Path) -> None:
    """Completed signature should mark onboarding complete when Slack user already exists."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService(webhook_token="hook-secret")
        fake_slack = _FakeSlackService(
            invite_status="already_in_workspace", invite_user_id="U42EXIST"
        )
        email_sender = _CaptureEmailSender()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            slack_service=fake_slack,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
            slack_invite_fallback_join_url="https://join.slack.com/t/hireme/shared_invite/test",
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Slack Existing Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Slack Existing User",
                email="slack-existing@example.com",
                linkedin_url="https://www.linkedin.com/in/slack-existing-user",
                portfolio_url="https://slack-existing.dev",
                github_url="https://github.com/slack-existing-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Slack Existing Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=7),
                base_salary="USD 151,000",
                compensation_structure="Base + bonus",
                equity_or_bonus="0.2% equity",
                reporting_manager="Engineering Manager",
                custom_terms=None,
            ),
        )
        await app_service.approve_offer_letter(application_id=created.id)
        processed = await app_service.handle_docusign_webhook(
            application_id=created.id,
            webhook_token="hook-secret",
            raw_body=b'{"event":"envelope-completed"}',
            content_type="application/json",
        )
        assert processed is True

        updated = await app_service.get_by_id(created.id)
        assert updated is not None
        assert updated.slack_invite_status == "already_in_workspace"
        assert updated.slack_onboarding_status == "onboarded"
        assert updated.slack_user_id == "U42EXIST"
        assert updated.slack_error is None
        assert len(email_sender.slack_invite_payloads) == 0

    asyncio.run(run())


def test_docusign_signed_event_uses_fallback_invite_link_email_when_slack_invite_fails(
    tmp_path: Path,
) -> None:
    """If Slack API invite fails, fallback invite link email should be sent."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService(webhook_token="hook-secret")
        fake_slack = _FakeSlackService(fail_invite=True)
        email_sender = _CaptureEmailSender()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            slack_service=fake_slack,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
            slack_invite_fallback_join_url="https://join.slack.com/t/hireme/shared_invite/test",
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Slack Fallback Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Slack Fallback User",
                email="slack-fallback@example.com",
                linkedin_url="https://www.linkedin.com/in/slack-fallback-user",
                portfolio_url="https://slack-fallback.dev",
                github_url="https://github.com/slack-fallback-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Slack Fallback Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=14),
                base_salary="USD 148,000",
                compensation_structure="Base + bonus",
                equity_or_bonus=None,
                reporting_manager="Engineering Manager",
                custom_terms=None,
            ),
        )
        await app_service.approve_offer_letter(application_id=created.id)

        processed = await app_service.handle_docusign_webhook(
            application_id=created.id,
            webhook_token="hook-secret",
            raw_body=b'{"event":"envelope-completed"}',
            content_type="application/json",
        )
        assert processed is True
        assert len(email_sender.slack_invite_payloads) == 1
        assert (
            email_sender.slack_invite_payloads[0].slack_invite_link
            == "https://join.slack.com/t/hireme/shared_invite/test"
        )

        updated = await app_service.get_by_id(created.id)
        assert updated is not None
        assert updated.slack_invite_status == "invite_link_sent"
        assert updated.applicant_status == "offer_letter_sign"

    asyncio.run(run())


def test_sync_signature_retries_slack_invite_after_previous_failure(tmp_path: Path) -> None:
    """Manual DocuSign sync should retry Slack invite for already-signed candidates."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService(webhook_token="hook-secret")
        fake_slack = _FakeSlackService(fail_invite=True)
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            slack_service=fake_slack,
            notification_config=NotificationRuntimeConfig(enabled=False),
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Slack Retry Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Slack Retry User",
                email="slack-retry@example.com",
                linkedin_url="https://www.linkedin.com/in/slack-retry-user",
                portfolio_url="https://slack-retry.dev",
                github_url="https://github.com/slack-retry-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Slack Retry Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=11),
                base_salary="USD 147,000",
                compensation_structure="Base + bonus",
                equity_or_bonus=None,
                reporting_manager="Engineering Manager",
                custom_terms=None,
            ),
        )
        await app_service.approve_offer_letter(application_id=created.id)
        await app_service.handle_docusign_webhook(
            application_id=created.id,
            webhook_token="hook-secret",
            raw_body=b'{"event":"envelope-completed"}',
            content_type="application/json",
        )
        after_failed_invite = await app_service.get_by_id(created.id)
        assert after_failed_invite is not None
        assert after_failed_invite.offer_letter_status == "signed"
        assert after_failed_invite.slack_invite_status == "failed"

        fake_slack.fail_invite = False
        synced = await app_service.sync_offer_letter_signature_status(application_id=created.id)
        assert synced is not None
        assert synced.slack_invite_status == "invited"
        assert synced.slack_onboarding_status == "invited"
        assert synced.slack_error is None
        assert len(fake_slack.invite_calls) == 1

    asyncio.run(run())


def test_slack_team_join_sends_ai_welcome_and_hr_notification(tmp_path: Path) -> None:
    """Slack team_join callback should DM candidate and notify HR."""

    async def run() -> None:
        fake_offer_service = _FakeOfferLetterService()
        fake_docusign = _FakeDocusignService(webhook_token="hook-secret")
        fake_slack = _FakeSlackService()
        fake_welcome = _FakeSlackWelcomeService()
        job_service, app_service = _build_service(
            tmp_path,
            offer_letter_service=fake_offer_service,
            docusign_service=fake_docusign,
            slack_service=fake_slack,
            slack_welcome_service=fake_welcome,
            notification_config=NotificationRuntimeConfig(enabled=False),
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Slack Join Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Slack Join User",
                email="slack-join@example.com",
                linkedin_url="https://www.linkedin.com/in/slack-join-user",
                portfolio_url="https://slack-join.dev",
                github_url="https://github.com/slack-join-user",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
        )
        await app_service.update_admin_review(
            application_id=created.id,
            updates={"interview_schedule_status": "interview_done"},
        )
        await app_service.record_manager_decision(
            application_id=created.id,
            decision="select",
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Slack Join Engineer II",
                start_date=datetime.now(tz=timezone.utc).date() + timedelta(days=12),
                base_salary="USD 149,000",
                compensation_structure="Base + bonus",
                equity_or_bonus="0.2% equity",
                reporting_manager="Hiring Manager",
                custom_terms=None,
            ),
        )
        await app_service.approve_offer_letter(application_id=created.id)
        await app_service.handle_docusign_webhook(
            application_id=created.id,
            webhook_token="hook-secret",
            raw_body=b'{"event":"envelope-completed"}',
            content_type="application/json",
        )

        team_join_payload = {
            "type": "event_callback",
            "event": {
                "type": "team_join",
                "user": {
                    "id": "U123JOIN",
                    "profile": {"email": "slack-join@example.com"},
                },
            },
        }
        response = await app_service.handle_slack_webhook(
            raw_body=json.dumps(team_join_payload).encode("utf-8"),
            headers={},
        )
        assert response == {"processed": True}
        assert len(fake_welcome.calls) == 1
        assert len(fake_slack.dm_calls) == 1
        assert len(fake_slack.hr_calls) == 1
        assert fake_slack.dm_calls[0][0] == "U123JOIN"

        updated = await app_service.get_by_id(created.id)
        assert updated is not None
        assert updated.slack_user_id == "U123JOIN"
        assert updated.slack_joined_at is not None
        assert updated.slack_welcome_sent_at is not None
        assert updated.slack_onboarding_status == "onboarded"
        assert updated.applicant_status == "offer_letter_sign"

    asyncio.run(run())


def test_slack_team_join_defer_enqueues_durable_job(tmp_path: Path) -> None:
    """Deferred Slack team_join handling should enqueue durable webhook job."""

    async def run() -> None:
        capture_queue = _CaptureWebhookEventQueuePublisher()
        _, app_service = _build_service(
            tmp_path,
            slack_service=_FakeSlackService(),
            webhook_event_queue_publisher=capture_queue,
            webhook_async_config=ApplicationRuntimeConfig.WebhookAsyncRuntimeConfig(
                enabled=True,
                use_queue=True,
                provider="sqs",
                queue_url="https://example.test/webhook-queue",
            ),
        )
        payload = {
            "type": "event_callback",
            "event_id": "EvSlack123",
            "event": {
                "type": "team_join",
                "event_ts": "1720000000.001",
                "user": {
                    "id": "UQ123",
                    "profile": {"email": "slack-join@example.com"},
                },
            },
        }

        response = await app_service.handle_slack_webhook(
            raw_body=json.dumps(payload).encode("utf-8"),
            headers={},
            defer_team_join_processing=True,
        )

        assert response == {"processed": True, "queued": True}
        assert len(capture_queue.jobs) == 1
        assert capture_queue.jobs[0].event_type == "slack_team_join"
        assert capture_queue.jobs[0].event_key == "slack:team_join:EvSlack123"
        assert capture_queue.jobs[0].payload["slack_user_id"] == "UQ123"

    asyncio.run(run())


def test_enqueue_confirmation_email_publishes_webhook_job(tmp_path: Path) -> None:
    """Confirmation email should be queued when webhook async queueing is enabled."""

    async def run() -> None:
        capture_queue = _CaptureWebhookEventQueuePublisher()
        job_service, app_service = _build_service(
            tmp_path,
            webhook_event_queue_publisher=capture_queue,
            webhook_async_config=ApplicationRuntimeConfig.WebhookAsyncRuntimeConfig(
                enabled=True,
                use_queue=True,
                provider="sqs",
                queue_url="https://example.test/webhook-queue",
            ),
            notification_config=NotificationRuntimeConfig(enabled=False),
        )
        opening = await _create_opening(
            job_service,
            role_title=f"Webhook Queue Engineer {uuid4().hex[:6]}",
        )
        created = await app_service.submit(
            payload=ApplicationCreatePayload(
                full_name="Queue Email Candidate",
                email="queue-email@example.com",
                linkedin_url="https://www.linkedin.com/in/queue-email",
                portfolio_url="https://queue-email.dev",
                github_url="https://github.com/queue-email",
                role_selection=opening.role_title,
            ),
            resume=_resume_file(),
            send_confirmation_email=False,
        )

        await app_service.enqueue_application_confirmation_email(application_id=created.id)

        assert len(capture_queue.jobs) == 1
        job = capture_queue.jobs[0]
        assert job.event_type == "application_confirmation_email"
        assert job.payload["application_id"] == str(created.id)
        assert job.event_key == f"application_confirmation_email:{created.id}"

    asyncio.run(run())


def test_slack_url_verification_returns_challenge_without_slack_service(
    tmp_path: Path,
) -> None:
    """Slack URL verification should succeed even before full Slack config."""

    async def run() -> None:
        _, app_service = _build_service(tmp_path)
        response = await app_service.handle_slack_webhook(
            raw_body=json.dumps(
                {
                    "type": "url_verification",
                    "challenge": "abc123",
                }
            ).encode("utf-8"),
            headers={},
        )
        assert response == {"challenge": "abc123"}

    asyncio.run(run())
