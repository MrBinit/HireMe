"""Tests for application submission and duplicate handling behavior."""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from starlette.datastructures import Headers, UploadFile

from app.core.runtime_config import (
    ApplicationRuntimeConfig,
    JobOpeningRuntimeConfig,
    NotificationRuntimeConfig,
    ParseRuntimeConfig,
)
from app.repositories.local_application_repository import LocalApplicationRepository
from app.repositories.local_job_opening_repository import LocalJobOpeningRepository
from app.schemas.application import ApplicationCreatePayload
from app.schemas.job_opening import JobOpeningCreatePayload
from app.services.application_service import ApplicationService, ApplicationValidationError
from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSender,
    NoopEmailSender,
)
from app.services.job_opening_service import JobOpeningService
from app.services.parse_queue import (
    NoopParseQueuePublisher,
    ParseQueuePublishError,
    ParseQueuePublisher,
    ResumeParseJob,
)
from app.services.resume_storage import LocalResumeStorage


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


class _CaptureEmailSender(EmailSender):
    """Test email sender that captures confirmation payloads."""

    def __init__(self) -> None:
        self.payloads = []

    async def send_application_confirmation(
        self,
        payload: ApplicationConfirmationEmail,
    ) -> None:
        self.payloads.append(payload)


def _build_service(
    tmp_path: Path,
    *,
    use_queue: bool = False,
    parse_queue_publisher: ParseQueuePublisher | None = None,
    notification_config: NotificationRuntimeConfig | None = None,
    email_sender: EmailSender | None = None,
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
        config=ApplicationRuntimeConfig(),
        resume_storage=LocalResumeStorage(tmp_path / "resumes"),
        parse_config=ParseRuntimeConfig(use_queue=use_queue),
        parse_queue_publisher=parse_queue_publisher or NoopParseQueuePublisher(),
        notification_config=notification_config or NotificationRuntimeConfig(enabled=False),
        email_sender=email_sender or NoopEmailSender(),
    )
    return job_service, app_service


async def _create_opening(job_service: JobOpeningService, *, role_title: str):
    """Create a single opening used by tests."""

    return await job_service.create(
        JobOpeningCreatePayload(
            role_title=role_title,
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
