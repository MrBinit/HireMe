"""Unit tests for interview scheduling orchestration service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.error import ApplicationValidationError
from app.core.runtime_config import SchedulingRuntimeConfig
from app.infra.google_calendar_client import CalendarHoldEvent
from app.schemas.application import ApplicationRecord, ResumeFileMeta
from app.schemas.job_opening import JobOpeningRecord
from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSender,
    InitialScreeningRejectionEmail,
    InterviewSlotOptionsEmail,
)
from app.services.interview_scheduling_service import InterviewSchedulingService


class _FakeApplicationRepository:
    def __init__(self, candidate: ApplicationRecord):
        self._candidate = candidate

    async def get_by_id(self, application_id):
        if application_id == self._candidate.id:
            return self._candidate
        return None

    async def update_admin_review(self, *, application_id, updates):
        if application_id != self._candidate.id:
            return False
        self._candidate = self._candidate.model_copy(update=updates)
        return True


class _FakeJobOpeningRepository:
    def __init__(self, opening: JobOpeningRecord):
        self._opening = opening

    async def get(self, opening_id):
        if opening_id == self._opening.id:
            return self._opening
        return None


class _FakeCalendarClient:
    def __init__(self):
        self.created: list[CalendarHoldEvent] = []

    async def list_busy_intervals(self, **kwargs):
        _ = kwargs
        return []

    async def create_hold_event(self, **kwargs):
        index = len(self.created) + 1
        event = CalendarHoldEvent(
            event_id=f"event-{index}",
            html_link=f"https://calendar.google.com/event?eid={index}",
            start_at=kwargs["start_at"],
            end_at=kwargs["end_at"],
        )
        self.created.append(event)
        return event

    async def delete_event(self, **kwargs):
        _ = kwargs


class _CaptureEmailSender(EmailSender):
    def __init__(self):
        self.payloads: list[InterviewSlotOptionsEmail] = []

    async def send_application_confirmation(self, payload: ApplicationConfirmationEmail) -> None:
        _ = payload

    async def send_initial_screening_rejection(
        self, payload: InitialScreeningRejectionEmail
    ) -> None:
        _ = payload

    async def send_interview_slot_options(self, payload: InterviewSlotOptionsEmail) -> None:
        self.payloads.append(payload)


def _candidate(applicant_status: str = "shortlisted") -> ApplicationRecord:
    now = datetime.now(tz=timezone.utc)
    return ApplicationRecord(
        id=uuid4(),
        job_opening_id=uuid4(),
        full_name="Jane Candidate",
        email="jane@example.com",
        linkedin_url="https://www.linkedin.com/in/jane",
        portfolio_url=None,
        github_url="https://github.com/jane",
        twitter_url=None,
        role_selection="AI Engineer",
        parse_result={"skills": ["Python"]},
        parsed_total_years_experience=2.0,
        parsed_search_text="python fastapi",
        parse_status="completed",
        evaluation_status="completed",
        applicant_status=applicant_status,
        ai_score=88.0,
        ai_screening_summary="strong fit",
        candidate_brief=None,
        online_research_summary=None,
        status_history=[],
        reference_status=False,
        resume=ResumeFileMeta(
            original_filename="resume.pdf",
            stored_filename="resume.pdf",
            storage_path="s3://bucket/resume.pdf",
            content_type="application/pdf",
            size_bytes=1024,
        ),
        created_at=now,
    )


def _opening(job_opening_id) -> JobOpeningRecord:
    now = datetime.now(tz=timezone.utc)
    return JobOpeningRecord(
        id=job_opening_id,
        role_title="AI Engineer",
        experience_level="mid",
        experience_range="1-3 years",
        responsibilities=["Build AI systems"],
        requirements=["Python", "FastAPI"],
        manager_email="b.sapkota.747@westcliff.edu",
        team="AI Platform",
        location="Remote",
        application_open_at=now - timedelta(days=1),
        application_close_at=now + timedelta(days=5),
        paused=False,
        created_at=now,
        updated_at=now,
    )


def test_interview_scheduling_creates_options_and_sends_email() -> None:
    """Shortlisted candidate should receive held interview options."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        service = InterviewSchedulingService(
            application_repository=_FakeApplicationRepository(candidate),  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=_FakeCalendarClient(),  # type: ignore[arg-type]
            email_sender=_CaptureEmailSender(),
            config=SchedulingRuntimeConfig(
                min_slots=3,
                max_slots=3,
                business_days_ahead=5,
                slot_duration_minutes=45,
                slot_step_minutes=45,
                business_hours_start_hour=9,
                business_hours_end_hour=17,
                min_notice_hours=1,
                timezone="Asia/Kathmandu",
            ),
        )
        payload = await service.create_options_for_candidate(application_id=candidate.id)
        assert payload["manager_email"] == "b.sapkota.747@westcliff.edu"
        assert len(payload["options"]) == 3

    asyncio.run(run())


def test_interview_scheduling_rejects_non_shortlisted_candidate() -> None:
    """Non-shortlisted candidate should not be scheduled."""

    async def run() -> None:
        candidate = _candidate(applicant_status="screened")
        service = InterviewSchedulingService(
            application_repository=_FakeApplicationRepository(candidate),  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=_FakeCalendarClient(),  # type: ignore[arg-type]
            email_sender=_CaptureEmailSender(),
            config=SchedulingRuntimeConfig(target_statuses=["shortlisted"]),
        )
        with pytest.raises(ApplicationValidationError):
            await service.create_options_for_candidate(application_id=candidate.id)

    asyncio.run(run())
