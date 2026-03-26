"""Tests for resume parse processor initial-screening behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.core.runtime_config import (
    ApplicationRuntimeConfig,
    JobOpeningRuntimeConfig,
    NotificationRuntimeConfig,
)
from app.repositories.local_application_repository import LocalApplicationRepository
from app.repositories.local_job_opening_repository import LocalJobOpeningRepository
from app.schemas.application import (
    ApplicationRecord,
    ResumeFileMeta,
    StatusHistoryEntry,
)
from app.schemas.job_opening import JobOpeningCreatePayload
from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSender,
    InitialScreeningRejectionEmail,
)
from app.services.job_opening_service import JobOpeningService
from app.services.parse_processor import ResumeParseProcessor


@dataclass
class _StructuredResult:
    """Test structured extractor output wrapper."""

    payload: dict

    def to_dict(self) -> dict:
        """Return test payload."""

        return self.payload


class _FakeStructuredExtractor:
    """Test structured extractor returning fixed output."""

    def __init__(self, payload: dict):
        self._payload = payload

    def extract(self, *, text: str, fallback_name: str) -> _StructuredResult:
        """Return prebuilt structured parse payload."""

        _ = (text, fallback_name)
        return _StructuredResult(payload=self._payload)


class _FakeExtractor:
    """Test resume extractor returning fixed plain text."""

    def __init__(self, text: str):
        self._text = text

    async def extract_text(self, storage_path: str) -> str:
        """Return fixture text for parsing."""

        _ = storage_path
        return self._text


class _CaptureEmailSender(EmailSender):
    """Test email sender capturing outbound notifications."""

    def __init__(self) -> None:
        self.confirmation_payloads: list[ApplicationConfirmationEmail] = []
        self.rejection_payloads: list[InitialScreeningRejectionEmail] = []

    async def send_application_confirmation(
        self,
        payload: ApplicationConfirmationEmail,
    ) -> None:
        """Capture confirmation email payload."""

        self.confirmation_payloads.append(payload)

    async def send_initial_screening_rejection(
        self,
        payload: InitialScreeningRejectionEmail,
    ) -> None:
        """Capture rejection email payload."""

        self.rejection_payloads.append(payload)


async def _create_opening(
    job_service: JobOpeningService,
    *,
    role_title: str,
) -> None:
    """Create one job opening for parse tests."""

    await job_service.create(
        JobOpeningCreatePayload(
            role_title=role_title,
            team="Platform",
            location="remote",
            experience_level="mid",
            experience_range="2-4 years",
            application_open_at=datetime.now(tz=timezone.utc) - timedelta(days=1),
            application_close_at=datetime.now(tz=timezone.utc) + timedelta(days=7),
            responsibilities=["Build backend APIs"],
            requirements=["Python", "FastAPI"],
        )
    )


def _build_application(role_title: str, email: str) -> ApplicationRecord:
    """Build one pending application record."""

    now = datetime.now(tz=timezone.utc)
    app_id = uuid4()
    return ApplicationRecord(
        id=app_id,
        job_opening_id=uuid4(),
        full_name="Test Candidate",
        email=email,
        linkedin_url="https://www.linkedin.com/in/test-candidate",
        portfolio_url="https://candidate.dev",
        github_url="https://github.com/candidate",
        twitter_url=None,
        role_selection=role_title,
        parse_result=None,
        parsed_total_years_experience=None,
        parsed_search_text=None,
        parse_status="pending",
        applicant_status="applied",
        rejection_reason=None,
        ai_score=None,
        ai_screening_summary=None,
        online_research_summary=None,
        status_history=[
            StatusHistoryEntry(
                status="applied",
                note="application submitted",
                changed_at=now,
                source="system",
            )
        ],
        reference_status=False,
        resume=ResumeFileMeta(
            original_filename="resume.pdf",
            stored_filename=f"{app_id}.pdf",
            storage_path=f"s3://hireme-cv-bucket/hireme/resumes/{app_id}.pdf",
            content_type="application/pdf",
            size_bytes=1200,
        ),
        created_at=now,
    )


def test_initial_screening_passes_when_experience_matches_and_skills_match(
    tmp_path: Path,
) -> None:
    """Candidate should pass on experience+skills even when keyword gate fails."""

    async def run() -> None:
        app_repo = LocalApplicationRepository(tmp_path / "applications.json")
        job_repo = LocalJobOpeningRepository(tmp_path / "job_openings.json")
        job_service = JobOpeningService(job_repo, JobOpeningRuntimeConfig())

        role_title = f"Backend Engineer {uuid4().hex[:6]}"
        await _create_opening(job_service, role_title=role_title)
        opening = await job_repo.find_by_role_title(role_title)
        assert opening is not None
        app_record = _build_application(role_title, "skills-pass@example.com").model_copy(
            update={"job_opening_id": opening.id}
        )
        created = await app_repo.create(app_record)

        processor = ResumeParseProcessor(
            repository=app_repo,
            job_opening_repository=job_repo,
            application_config=ApplicationRuntimeConfig(
                prefilter_min_keyword_matches=2,
                prefilter_min_skill_matches=1,
            ),
            extractor=_FakeExtractor("Python backend profile"),
            structured_extractor=_FakeStructuredExtractor(
                {
                    "skills": ["Python"],
                    "total_years_experience": 3.0,
                    "education": [],
                    "work_history": [],
                }
            ),
            llm_fallback_min_chars=400,
            prefilter_max_search_text_chars=8000,
            notification_config=NotificationRuntimeConfig(enabled=False),
            email_sender=_CaptureEmailSender(),
        )

        processed = await processor.process(created.id)
        assert processed is True

        saved = await app_repo.get_by_id(created.id)
        assert saved is not None
        assert saved.applicant_status == "screened"
        assert isinstance(saved.parse_result, dict)
        screening = saved.parse_result.get("initial_screening")
        assert isinstance(screening, dict)
        assert screening.get("experience_pass") is True
        assert screening.get("skills_pass") is True
        assert screening.get("keyword_pass") is False
        assert screening.get("passed") is True

    asyncio.run(run())


def test_initial_screening_failure_sends_rejection_email(tmp_path: Path) -> None:
    """Candidate failing initial screening should be rejected and receive rejection email."""

    async def run() -> None:
        app_repo = LocalApplicationRepository(tmp_path / "applications.json")
        job_repo = LocalJobOpeningRepository(tmp_path / "job_openings.json")
        job_service = JobOpeningService(job_repo, JobOpeningRuntimeConfig())

        role_title = f"Backend Engineer {uuid4().hex[:6]}"
        await _create_opening(job_service, role_title=role_title)
        opening = await job_repo.find_by_role_title(role_title)
        assert opening is not None

        record = _build_application(role_title, "reject-me@example.com").model_copy(
            update={"job_opening_id": opening.id}
        )
        created = await app_repo.create(record)
        email_sender = _CaptureEmailSender()

        processor = ResumeParseProcessor(
            repository=app_repo,
            job_opening_repository=job_repo,
            application_config=ApplicationRuntimeConfig(
                prefilter_min_keyword_matches=2,
                prefilter_min_skill_matches=1,
            ),
            extractor=_FakeExtractor("Spreadsheet-heavy profile"),
            structured_extractor=_FakeStructuredExtractor(
                {
                    "skills": ["Excel"],
                    "total_years_experience": 1.0,
                    "education": [],
                    "work_history": [],
                }
            ),
            llm_fallback_min_chars=400,
            prefilter_max_search_text_chars=8000,
            notification_config=NotificationRuntimeConfig(enabled=True),
            email_sender=email_sender,
        )

        processed = await processor.process(created.id)
        assert processed is True

        saved = await app_repo.get_by_id(created.id)
        assert saved is not None
        assert saved.applicant_status == "rejected"
        assert saved.rejection_reason == "Candidate failed in initial screening."
        assert len(email_sender.rejection_payloads) == 1
        assert email_sender.rejection_payloads[0].candidate_email == "reject-me@example.com"
        assert email_sender.confirmation_payloads == []

    asyncio.run(run())
