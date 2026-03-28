"""Unit tests for interview scheduling orchestration service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from app.core.error import ApplicationValidationError
from app.core.security import decode_interview_action_token
from app.core.runtime_config import SchedulingRuntimeConfig, SecurityRuntimeConfig
from app.infra.google_calendar_client import CalendarHoldEvent
from app.schemas.application import ApplicationRecord, ResumeFileMeta
from app.schemas.job_opening import JobOpeningRecord
from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSender,
    InitialScreeningRejectionEmail,
    InterviewRescheduleOptionsEmail,
    InterviewSlotOptionsEmail,
)
from app.services.fireflies_service import FirefliesService, FirefliesTranscriptMatch
from app.services.interview_scheduling_service import InterviewSchedulingService
from app.scripts.interview_hold_expiry_worker import InterviewHoldExpiryWorker


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
        safe_updates = {k: v for k, v in updates.items() if k in set(self._candidate.model_fields)}
        self._candidate = self._candidate.model_copy(update=safe_updates)
        return True

    async def transition_interview_schedule_status(
        self, *, application_id, from_statuses, to_status
    ):
        if application_id != self._candidate.id:
            return False
        if self._candidate.interview_schedule_status not in set(from_statuses):
            return False
        self._candidate = self._candidate.model_copy(
            update={"interview_schedule_status": to_status}
        )
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
        self.deleted_event_ids: list[str] = []
        self.confirmed_event_ids: list[str] = []

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
        event_id = kwargs.get("event_id")
        if isinstance(event_id, str):
            self.deleted_event_ids.append(event_id)

    async def confirm_hold_event(self, **kwargs):
        event_id = kwargs["event_id"]
        self.confirmed_event_ids.append(event_id)
        for event in self.created:
            if event.event_id == event_id:
                return event
        raise RuntimeError("event not found")


class _CaptureEmailSender(EmailSender):
    def __init__(self):
        self.payloads: list[InterviewSlotOptionsEmail] = []
        self.reminder_payloads: list[InterviewSlotOptionsEmail] = []
        self.manager_reschedule_payloads: list[InterviewRescheduleOptionsEmail] = []
        self.confirmed_payloads = []

    async def send_application_confirmation(self, payload: ApplicationConfirmationEmail) -> None:
        _ = payload

    async def send_initial_screening_rejection(
        self, payload: InitialScreeningRejectionEmail
    ) -> None:
        _ = payload

    async def send_interview_slot_options(self, payload: InterviewSlotOptionsEmail) -> None:
        self.payloads.append(payload)

    async def send_interview_slot_reminder(self, payload: InterviewSlotOptionsEmail) -> None:
        self.reminder_payloads.append(payload)

    async def send_interview_booking_confirmed(self, payload) -> None:
        self.confirmed_payloads.append(payload)

    async def send_interview_reschedule_options_to_manager(
        self, payload: InterviewRescheduleOptionsEmail
    ) -> None:
        self.manager_reschedule_payloads.append(payload)


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
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=_FakeApplicationRepository(candidate),  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=_FakeCalendarClient(),  # type: ignore[arg-type]
            email_sender=email_sender,
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
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )
        payload = await service.create_options_for_candidate(application_id=candidate.id)
        assert payload["manager_email"] == "b.sapkota.747@westcliff.edu"
        assert len(payload["options"]) == 3
        assert len(email_sender.payloads) == 1
        assert len(email_sender.payloads[0].slot_option_links) == 3
        assert len(email_sender.payloads[0].action_links) == 2
        assert email_sender.payloads[0].action_links[0][0] == "Ask for another date"
        assert email_sender.payloads[0].action_links[1][0] == "Cancel these slots"
        first_action_link = email_sender.payloads[0].action_links[0][1]
        second_action_link = email_sender.payloads[0].action_links[1][1]
        assert first_action_link != second_action_link
        first_token = parse_qs(urlparse(first_action_link).query).get("token", [""])[0]
        second_token = parse_qs(urlparse(second_action_link).query).get("token", [""])[0]
        first_claims = decode_interview_action_token(
            token=first_token,
            secret="test-secret",
            config=SecurityRuntimeConfig(),
        )
        second_claims = decode_interview_action_token(
            token=second_token,
            secret="test-secret",
            config=SecurityRuntimeConfig(),
        )
        assert first_claims.action == "request_reschedule"
        assert second_claims.action == "cancel_options"

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
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )
        with pytest.raises(ApplicationValidationError):
            await service.create_options_for_candidate(application_id=candidate.id)

    asyncio.run(run())


def test_confirm_interview_slot_books_selected_and_releases_others() -> None:
    """Selecting one offered option confirms it and deletes non-selected hold events."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
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
                calendar_send_updates_mode="none",
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )

        await service.create_options_for_candidate(application_id=candidate.id)
        updated = await service.confirm_candidate_slot(
            application_id=candidate.id,
            candidate_email="jane@example.com",
            option_number=1,
        )
        assert updated["selected_option_number"] == 1
        assert "confirmed_event_id" in updated
        assert updated.get("calendar_invite_response_status") == "needs_action"
        assert updated.get("calendar_invite_response_source") == "calendar_invite_sent"
        assert isinstance(updated.get("calendar_invite_response_at"), str)
        assert len(calendar_client.confirmed_event_ids) == 1
        assert len(calendar_client.deleted_event_ids) == 2
        assert len(email_sender.confirmed_payloads) == 2

    asyncio.run(run())


def test_expire_holds_releases_all_options_after_expiry() -> None:
    """Expired interview holds are released and status is marked expired."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
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
                hold_expiry_hours=1,
                timezone="Asia/Kathmandu",
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )
        await service.create_options_for_candidate(application_id=candidate.id)
        expired = await service.expire_candidate_holds(application_id=candidate.id, force=True)
        assert expired is True
        assert len(calendar_client.deleted_event_ids) == 3

    asyncio.run(run())


def test_confirm_interview_slot_attaches_fireflies_tracking_payload() -> None:
    """Booking confirmation should persist Fireflies sync metadata when enabled."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
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
                calendar_send_updates_mode="none",
                fireflies={"enabled": True},
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
            fireflies_service=FirefliesService(
                api_key="test-fireflies-key",
                config=SchedulingRuntimeConfig.FirefliesRuntimeConfig(enabled=True),
            ),
        )

        await service.create_options_for_candidate(application_id=candidate.id)
        updated = await service.confirm_candidate_slot(
            application_id=candidate.id,
            candidate_email="jane@example.com",
            option_number=1,
        )

        fireflies_payload = updated.get("fireflies")
        assert isinstance(fireflies_payload, dict)
        assert fireflies_payload.get("status") == "scheduled"
        assert fireflies_payload.get("manager_email") == "b.sapkota.747@westcliff.edu"
        assert fireflies_payload.get("candidate_email") == "jane@example.com"
        assert isinstance(updated.get("confirmed_start_at"), str)
        assert isinstance(updated.get("confirmed_end_at"), str)

    asyncio.run(run())


def test_fireflies_mock_sync_writes_transcript_columns_and_marks_interview_done() -> None:
    """Mock Fireflies sync should persist transcript fields and mark interview done."""

    async def run() -> None:
        candidate = _candidate(applicant_status="in_interview")
        candidate = candidate.model_copy(
            update={
                "interview_schedule_status": "interview_booked",
                "interview_calendar_email": "b.sapkota.747@westcliff.edu",
                "interview_schedule_options": {
                    "confirmed_event_id": "event-123",
                    "confirmed_meeting_link": "https://meet.google.com/abc-defg-hij",
                    "confirmed_manager_email": "b.sapkota.747@westcliff.edu",
                    "confirmed_start_at": datetime.now(tz=timezone.utc).isoformat(),
                    "confirmed_end_at": (
                        datetime.now(tz=timezone.utc) + timedelta(minutes=45)
                    ).isoformat(),
                },
            }
        )
        application_repository = _FakeApplicationRepository(candidate)
        config = SchedulingRuntimeConfig(
            booked_status="interview_booked",
            fireflies={
                "enabled": True,
                "mock_mode": True,
                "owner_email": "b.sapkota.747@westcliff.edu",
                "completed_schedule_status": "interview_done",
            },
        )
        worker = InterviewHoldExpiryWorker(
            config=config,
            session_factory=None,  # type: ignore[arg-type]
            scheduling_service=None,  # type: ignore[arg-type]
            application_repository=application_repository,  # type: ignore[arg-type]
            fireflies_service=FirefliesService(
                api_key="mock-key",
                config=config.fireflies,
            ),
            email_sender=_CaptureEmailSender(),
        )

        async def _fake_fetch_ids() -> list:
            return [candidate.id]

        worker._fetch_fireflies_sync_application_ids = _fake_fetch_ids  # type: ignore[method-assign]
        updated_count = await worker._sync_fireflies_transcripts()

        refreshed = await application_repository.get_by_id(candidate.id)
        assert updated_count == 1
        assert refreshed is not None
        assert refreshed.interview_schedule_status == "interview_done"
        assert refreshed.interview_transcript_status == "completed"
        assert isinstance(refreshed.interview_transcript_url, str)
        assert "mock-" in refreshed.interview_transcript_url
        assert isinstance(refreshed.interview_transcript_summary, str)
        assert "Mock transcript summary" in refreshed.interview_transcript_summary
        assert refreshed.interview_transcript_synced_at is not None

    asyncio.run(run())


def test_fireflies_sync_keeps_polling_when_matched_transcript_content_is_empty() -> None:
    """Sync should not auto-complete when Fireflies match has no usable transcript content."""

    async def run() -> None:
        candidate = _candidate(applicant_status="in_interview")
        now_utc = datetime.now(tz=timezone.utc)
        candidate = candidate.model_copy(
            update={
                "interview_schedule_status": "interview_booked",
                "interview_calendar_email": "b.sapkota.747@westcliff.edu",
                "interview_schedule_options": {
                    "confirmed_event_id": "event-xyz",
                    "confirmed_meeting_link": "https://meet.google.com/xyz-xyzz-xyz",
                    "confirmed_manager_email": "b.sapkota.747@westcliff.edu",
                    "confirmed_start_at": (now_utc - timedelta(minutes=70)).isoformat(),
                    "confirmed_end_at": (now_utc - timedelta(minutes=25)).isoformat(),
                },
            }
        )
        application_repository = _FakeApplicationRepository(candidate)
        config = SchedulingRuntimeConfig(
            booked_status="interview_booked",
            fireflies={
                "enabled": True,
                "mock_mode": False,
                "owner_email": "b.sapkota.747@westcliff.edu",
                "completed_schedule_status": "interview_done",
                "transcript_poll_delay_minutes": 0,
            },
        )
        fireflies_service = FirefliesService(
            api_key="real-key",
            config=config.fireflies,
        )

        async def _fake_request_live_capture(*, meeting_link: str, title: str | None):
            _ = (meeting_link, title)
            return {"success": True, "message": "ok", "error": None}

        async def _fake_find_best_transcript(**kwargs):
            _ = kwargs
            return FirefliesTranscriptMatch(
                transcript_id="trx-empty",
                title="Interview",
                transcript_url=None,
                video_url=None,
                meeting_link="https://meet.google.com/xyz-xyzz-xyz",
                occurred_at=datetime.now(tz=timezone.utc),
                summary_text=None,
                action_items=[],
                keywords=[],
                raw={"source": "test"},
            )

        fireflies_service.request_live_capture = _fake_request_live_capture  # type: ignore[method-assign]
        fireflies_service.find_best_transcript = _fake_find_best_transcript  # type: ignore[method-assign]

        worker = InterviewHoldExpiryWorker(
            config=config,
            session_factory=None,  # type: ignore[arg-type]
            scheduling_service=None,  # type: ignore[arg-type]
            application_repository=application_repository,  # type: ignore[arg-type]
            fireflies_service=fireflies_service,
            email_sender=_CaptureEmailSender(),
        )

        async def _fake_fetch_ids() -> list:
            return [candidate.id]

        worker._fetch_fireflies_sync_application_ids = _fake_fetch_ids  # type: ignore[method-assign]
        updated_count = await worker._sync_fireflies_transcripts()

        refreshed = await application_repository.get_by_id(candidate.id)
        assert updated_count == 1
        assert refreshed is not None
        assert refreshed.interview_transcript_status == "processing"
        assert refreshed.interview_transcript_url is None
        assert refreshed.interview_transcript_summary is None
        options = refreshed.interview_schedule_options
        assert isinstance(options, dict)
        fireflies_payload = options.get("fireflies")
        assert isinstance(fireflies_payload, dict)
        assert fireflies_payload.get("status") == "processing"
        transcript_payload = fireflies_payload.get("transcript")
        assert transcript_payload is None
        transcript_sync = fireflies_payload.get("transcript_sync")
        assert isinstance(transcript_sync, dict)
        assert transcript_sync.get("status") == "polling"
        assert transcript_sync.get("last_error") == (
            "matched Fireflies transcript has no transcript content yet"
        )

    asyncio.run(run())


def test_confirm_recover_stale_interview_confirming_status() -> None:
    """A stale interview_confirming state should recover and allow confirmation."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
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
                calendar_send_updates_mode="none",
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )

        await service.create_options_for_candidate(application_id=candidate.id)
        stale_sent_at = datetime.now(tz=timezone.utc) - timedelta(minutes=2)
        await application_repository.update_admin_review(
            application_id=candidate.id,
            updates={
                "interview_schedule_status": "interview_confirming",
                "interview_schedule_sent_at": stale_sent_at,
            },
        )

        updated = await service.confirm_candidate_slot(
            application_id=candidate.id,
            candidate_email="jane@example.com",
            option_number=1,
        )

        assert updated["selected_option_number"] == 1
        assert updated.get("confirmed_event_id")
        assert len(calendar_client.confirmed_event_ids) == 1
        assert len(calendar_client.deleted_event_ids) == 2
        assert len(email_sender.confirmed_payloads) == 2

    asyncio.run(run())


def test_send_reminder_once_for_pending_interview_options() -> None:
    """Reminder should be sent once and persist reminder_sent_at marker."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
            config=SchedulingRuntimeConfig(
                min_slots=3,
                max_slots=5,
                business_days_ahead=5,
                slot_duration_minutes=45,
                slot_step_minutes=30,
                business_hours_start_hour=9,
                business_hours_end_hour=17,
                min_notice_hours=1,
                timezone="Asia/Kathmandu",
                reminder_after_hours=1,
                hold_expiry_hours=48,
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )

        await service.create_options_for_candidate(application_id=candidate.id)
        sent = await service.send_reminder_for_candidate(application_id=candidate.id)
        sent_again = await service.send_reminder_for_candidate(application_id=candidate.id)

        refreshed = await application_repository.get_by_id(candidate.id)
        assert sent is True
        assert sent_again is False
        assert len(email_sender.reminder_payloads) == 1
        assert refreshed is not None
        assert isinstance(refreshed.interview_schedule_options, dict)
        assert isinstance(refreshed.interview_schedule_options.get("reminder_sent_at"), str)

    asyncio.run(run())


def test_candidate_reschedule_request_sends_manager_alternatives() -> None:
    """Candidate reschedule request should send manager alternative options."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
            config=SchedulingRuntimeConfig(
                min_slots=3,
                max_slots=5,
                business_days_ahead=5,
                slot_duration_minutes=45,
                slot_step_minutes=30,
                business_hours_start_hour=9,
                business_hours_end_hour=17,
                min_notice_hours=1,
                timezone="Asia/Kathmandu",
                calendar_send_updates_mode="none",
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )
        await service.create_options_for_candidate(application_id=candidate.id)
        await service.confirm_candidate_slot(
            application_id=candidate.id,
            candidate_email="jane@example.com",
            option_number=1,
        )
        updated = await service.request_reschedule(
            application_id=candidate.id,
            actor="candidate",
            candidate_email="jane@example.com",
        )

        assert isinstance(updated.get("reschedule"), dict)
        assert int(updated["reschedule"]["round"]) == 1
        assert len(email_sender.manager_reschedule_payloads) == 1

    asyncio.run(run())


def test_candidate_can_request_reschedule_from_pending_options_email() -> None:
    """Candidate action-link reschedule should work before initial slot confirmation."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
            config=SchedulingRuntimeConfig(
                min_slots=3,
                max_slots=5,
                business_days_ahead=5,
                slot_duration_minutes=45,
                slot_step_minutes=30,
                business_hours_start_hour=9,
                business_hours_end_hour=17,
                min_notice_hours=1,
                timezone="Asia/Kathmandu",
                calendar_send_updates_mode="none",
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )

        await service.create_options_for_candidate(application_id=candidate.id)
        updated = await service.request_reschedule(
            application_id=candidate.id,
            actor="candidate",
            candidate_email="jane@example.com",
        )

        assert isinstance(updated.get("reschedule"), dict)
        assert int(updated["reschedule"]["round"]) == 1
        assert len(email_sender.manager_reschedule_payloads) == 1
        # Existing pending holds should be released before generating alternatives.
        assert len(calendar_client.deleted_event_ids) >= 3
        refreshed = await application_repository.get_by_id(candidate.id)
        assert refreshed is not None
        assert refreshed.interview_schedule_status == "interview_reschedule_options_sent"

    asyncio.run(run())


def test_candidate_can_cancel_pending_options_from_email_action() -> None:
    """Candidate cancel action should release held options and mark status expired."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
            config=SchedulingRuntimeConfig(
                min_slots=3,
                max_slots=5,
                business_days_ahead=5,
                slot_duration_minutes=45,
                slot_step_minutes=30,
                business_hours_start_hour=9,
                business_hours_end_hour=17,
                min_notice_hours=1,
                timezone="Asia/Kathmandu",
                calendar_send_updates_mode="none",
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )

        await service.create_options_for_candidate(application_id=candidate.id)
        updated = await service.cancel_pending_options(
            application_id=candidate.id,
            actor="candidate",
            candidate_email="jane@example.com",
        )

        assert isinstance(updated, dict)
        assert isinstance(updated.get("expired_at"), str)
        refreshed = await application_repository.get_by_id(candidate.id)
        assert refreshed is not None
        assert refreshed.interview_schedule_status == "interview_expired"
        assert len(calendar_client.deleted_event_ids) >= 3

    asyncio.run(run())


def test_manager_reject_then_accept_reschedule_round() -> None:
    """Manager can reject one round then accept next generated alternatives."""

    async def run() -> None:
        candidate = _candidate(applicant_status="shortlisted")
        application_repository = _FakeApplicationRepository(candidate)
        calendar_client = _FakeCalendarClient()
        email_sender = _CaptureEmailSender()
        service = InterviewSchedulingService(
            application_repository=application_repository,  # type: ignore[arg-type]
            job_opening_repository=_FakeJobOpeningRepository(  # type: ignore[arg-type]
                _opening(candidate.job_opening_id)
            ),
            calendar_client=calendar_client,  # type: ignore[arg-type]
            email_sender=email_sender,
            config=SchedulingRuntimeConfig(
                min_slots=3,
                max_slots=5,
                business_days_ahead=5,
                slot_duration_minutes=45,
                slot_step_minutes=30,
                business_hours_start_hour=9,
                business_hours_end_hour=17,
                min_notice_hours=1,
                timezone="Asia/Kathmandu",
                calendar_send_updates_mode="none",
            ),
            security_config=SecurityRuntimeConfig(),
            confirmation_token_secret="test-secret",
        )
        await service.create_options_for_candidate(application_id=candidate.id)
        await service.confirm_candidate_slot(
            application_id=candidate.id,
            candidate_email="jane@example.com",
            option_number=1,
        )
        updated = await service.request_reschedule(
            application_id=candidate.id,
            actor="manager",
        )
        assert int(updated["reschedule"]["round"]) == 1

        updated = await service.process_manager_reschedule_decision(
            application_id=candidate.id,
            decision="reject",
            round_number=1,
        )
        assert int(updated["reschedule"]["round"]) == 2
        assert len(email_sender.manager_reschedule_payloads) >= 2

        updated = await service.process_manager_reschedule_decision(
            application_id=candidate.id,
            decision="accept",
            round_number=2,
            option_number=1,
        )
        assert updated.get("confirmed_event_id")
        assert updated.get("calendar_invite_response_status") == "needs_action"
        assert updated.get("calendar_invite_response_source") == "calendar_invite_sent"
        assert isinstance(updated.get("calendar_invite_response_at"), str)
        refreshed = await application_repository.get_by_id(candidate.id)
        assert refreshed is not None
        assert refreshed.interview_schedule_status == "interview_booked"

    asyncio.run(run())
