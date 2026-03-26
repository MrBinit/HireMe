"""Interview scheduling orchestration for shortlisted candidates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.error import ApplicationValidationError
from app.core.runtime_config import SchedulingRuntimeConfig
from app.infra.google_calendar_client import (
    CalendarBusyInterval,
    CalendarHoldEvent,
    GoogleCalendarApiError,
    GoogleCalendarClient,
)
from app.repositories.application_repository import ApplicationRepository
from app.repositories.job_opening_repository import JobOpeningRepository
from app.services.email_sender import EmailSendError, EmailSender, InterviewSlotOptionsEmail

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateSlot:
    """One interview slot candidate in UTC."""

    start_at: datetime
    end_at: datetime


class InterviewSchedulingService:
    """Generate interview options, create tentative holds, and notify candidate."""

    def __init__(
        self,
        *,
        application_repository: ApplicationRepository,
        job_opening_repository: JobOpeningRepository,
        calendar_client: GoogleCalendarClient,
        email_sender: EmailSender,
        config: SchedulingRuntimeConfig,
    ) -> None:
        """Initialize scheduling dependencies."""

        self._application_repository = application_repository
        self._job_opening_repository = job_opening_repository
        self._calendar_client = calendar_client
        self._email_sender = email_sender
        self._config = config

    async def create_options_for_candidate(self, *, application_id: UUID) -> dict:
        """Create and email 3-5 held interview options for one shortlisted candidate."""

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            raise ApplicationValidationError("candidate application not found")
        if candidate.applicant_status not in set(self._config.target_statuses):
            raise ApplicationValidationError(
                f"candidate status {candidate.applicant_status} is not eligible for scheduling"
            )
        if (
            candidate.interview_schedule_status
            in {"interview_options_sent", "interview_email_sent", "options_sent"}
            and isinstance(candidate.interview_schedule_options, dict)
            and candidate.interview_schedule_options
        ):
            return candidate.interview_schedule_options

        opening = await self._job_opening_repository.get(candidate.job_opening_id)
        if opening is None:
            raise ApplicationValidationError("job opening not found for candidate")
        manager_email = opening.manager_email.strip().lower()
        if "@" not in manager_email:
            raise ApplicationValidationError("job opening manager_email is missing or invalid")
        delegated_user = self._delegated_user_for_manager(manager_email)

        now_utc = datetime.now(tz=timezone.utc)
        window_start, window_end = self._compute_time_window(now_utc)
        busy_intervals = await self._calendar_client.list_busy_intervals(
            calendar_id=manager_email,
            time_min=window_start,
            time_max=window_end,
            delegated_user=delegated_user,
        )
        free_slots = self._select_free_slots(
            now_utc=now_utc,
            busy_intervals=busy_intervals,
        )
        if len(free_slots) < self._config.min_slots:
            raise ApplicationValidationError(
                "insufficient free interview slots found in manager calendar"
            )

        hold_expires_at = now_utc + timedelta(hours=self._config.hold_expiry_hours)
        hold_events = await self._create_holds(
            application_id=application_id,
            manager_email=manager_email,
            delegated_user=delegated_user,
            candidate_name=candidate.full_name,
            role_title=candidate.role_selection,
            hold_expires_at=hold_expires_at,
            slots=free_slots,
        )
        if len(hold_events) < self._config.min_slots:
            await self._cleanup_holds(
                manager_email=manager_email,
                delegated_user=delegated_user,
                hold_events=hold_events,
            )
            raise ApplicationValidationError(
                "insufficient hold events created due slot conflicts; retry scheduling"
            )

        options_payload = self._build_options_payload(
            manager_email=manager_email,
            hold_expires_at=hold_expires_at,
            hold_events=hold_events,
        )
        email_payload = self._build_email_payload(
            candidate_name=candidate.full_name,
            candidate_email=str(candidate.email),
            role_title=candidate.role_selection,
            hold_events=hold_events,
            hold_expires_at=hold_expires_at,
        )
        try:
            await self._email_sender.send_interview_slot_options(email_payload)
        except (EmailSendError, Exception):
            await self._cleanup_holds(
                manager_email=manager_email,
                delegated_user=delegated_user,
                hold_events=hold_events,
            )
            raise

        await self._application_repository.update_admin_review(
            application_id=application_id,
            updates={
                "interview_schedule_status": "interview_options_sent",
                "interview_schedule_options": options_payload,
                "interview_schedule_sent_at": now_utc,
                "interview_hold_expires_at": hold_expires_at,
                "interview_calendar_email": manager_email,
                "interview_schedule_error": None,
            },
        )
        logger.info(
            "interview options generated application_id=%s manager=%s options=%s",
            application_id,
            manager_email,
            len(hold_events),
        )
        return options_payload

    def _compute_time_window(self, now_utc: datetime) -> tuple[datetime, datetime]:
        """Return UTC window bounds for next N business days."""

        tz = ZoneInfo(self._config.timezone)
        local_now = now_utc.astimezone(tz)
        business_days = self._collect_business_days(
            start_date=local_now.date(),
            count=self._config.business_days_ahead,
        )
        if not business_days:
            raise ApplicationValidationError("could not compute business-day interview window")

        first_day = business_days[0]
        last_day = business_days[-1]
        window_start_local = datetime.combine(
            first_day,
            time(hour=self._config.business_hours_start_hour, minute=0),
            tzinfo=tz,
        )
        window_end_local = datetime.combine(
            last_day,
            time(hour=self._config.business_hours_end_hour, minute=0),
            tzinfo=tz,
        )
        return window_start_local.astimezone(timezone.utc), window_end_local.astimezone(
            timezone.utc
        )

    def _select_free_slots(
        self,
        *,
        now_utc: datetime,
        busy_intervals: list[CalendarBusyInterval],
    ) -> list[CandidateSlot]:
        """Generate and filter free candidate slots, capped to max_slots."""

        tz = ZoneInfo(self._config.timezone)
        min_start_local = now_utc.astimezone(tz) + timedelta(hours=self._config.min_notice_hours)
        duration = timedelta(minutes=self._config.slot_duration_minutes)
        step = timedelta(minutes=self._config.slot_step_minutes)

        candidates: list[CandidateSlot] = []
        for day in self._collect_business_days(
            start_date=min_start_local.date(),
            count=self._config.business_days_ahead,
        ):
            day_start_local = datetime.combine(
                day,
                time(hour=self._config.business_hours_start_hour, minute=0),
                tzinfo=tz,
            )
            day_end_local = datetime.combine(
                day,
                time(hour=self._config.business_hours_end_hour, minute=0),
                tzinfo=tz,
            )
            start_local = max(day_start_local, min_start_local)
            start_local = self._ceil_to_slot_step(start_local, self._config.slot_step_minutes)
            cursor = start_local
            while cursor + duration <= day_end_local:
                slot = CandidateSlot(
                    start_at=cursor.astimezone(timezone.utc),
                    end_at=(cursor + duration).astimezone(timezone.utc),
                )
                if not self._overlaps_any(slot, busy_intervals):
                    candidates.append(slot)
                    if len(candidates) >= self._config.max_slots:
                        return candidates
                cursor = cursor + step
        return candidates

    async def _create_holds(
        self,
        *,
        application_id: UUID,
        manager_email: str,
        delegated_user: str | None,
        candidate_name: str,
        role_title: str,
        hold_expires_at: datetime,
        slots: list[CandidateSlot],
    ) -> list[CalendarHoldEvent]:
        """Create blocking hold events for selected slots."""

        holds: list[CalendarHoldEvent] = []
        for slot in slots:
            # Re-check exact slot just before creation to reduce race conflicts.
            busy_now = await self._calendar_client.list_busy_intervals(
                calendar_id=manager_email,
                time_min=slot.start_at,
                time_max=slot.end_at,
                delegated_user=delegated_user,
            )
            if self._overlaps_any(slot, busy_now):
                continue

            title = self._config.hold_event_title_template.format(
                candidate_name=candidate_name,
                role_title=role_title,
            )
            description = self._config.hold_event_description_template.format(
                application_id=str(application_id),
                candidate_name=candidate_name,
                role_title=role_title,
                hold_expires_at=hold_expires_at.isoformat(),
            )
            event = await self._calendar_client.create_hold_event(
                calendar_id=manager_email,
                delegated_user=delegated_user,
                title=title,
                description=description,
                start_at=slot.start_at,
                end_at=slot.end_at,
                timezone_name=self._config.timezone,
                extended_private_properties={
                    "hireme_application_id": str(application_id),
                    "hireme_hold_expires_at": hold_expires_at.isoformat(),
                },
            )
            holds.append(event)
            if len(holds) >= self._config.max_slots:
                break
        return holds

    async def _cleanup_holds(
        self,
        *,
        manager_email: str,
        delegated_user: str | None,
        hold_events: list[CalendarHoldEvent],
    ) -> None:
        """Best-effort cleanup for hold events created before a downstream failure."""

        for hold in hold_events:
            try:
                await self._calendar_client.delete_event(
                    calendar_id=manager_email,
                    delegated_user=delegated_user,
                    event_id=hold.event_id,
                )
            except GoogleCalendarApiError:
                logger.exception(
                    "failed to cleanup hold event event_id=%s manager=%s",
                    hold.event_id,
                    manager_email,
                )

    def _build_options_payload(
        self,
        *,
        manager_email: str,
        hold_expires_at: datetime,
        hold_events: list[CalendarHoldEvent],
    ) -> dict:
        """Build compact structured JSON persisted on candidate record."""

        return {
            "manager_email": manager_email,
            "timezone": self._config.timezone,
            "hold_expires_at": hold_expires_at.isoformat(),
            "options": [
                {
                    "option_number": index,
                    "start_at": event.start_at.isoformat(),
                    "end_at": event.end_at.isoformat(),
                    "hold_event_id": event.event_id,
                    "hold_event_link": event.html_link,
                }
                for index, event in enumerate(hold_events, start=1)
            ],
        }

    def _build_email_payload(
        self,
        *,
        candidate_name: str,
        candidate_email: str,
        role_title: str,
        hold_events: list[CalendarHoldEvent],
        hold_expires_at: datetime,
    ) -> InterviewSlotOptionsEmail:
        """Render email payload with numbered human-readable options."""

        tz = ZoneInfo(self._config.timezone)
        options: list[str] = []
        for index, event in enumerate(hold_events, start=1):
            start_local = event.start_at.astimezone(tz)
            end_local = event.end_at.astimezone(tz)
            options.append(
                f"Option {index}: {start_local:%a, %d %b %Y %I:%M %p} - "
                f"{end_local:%I:%M %p} ({self._config.timezone})"
            )

        return InterviewSlotOptionsEmail(
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            role_title=role_title,
            hold_expires_at=hold_expires_at.astimezone(tz).strftime("%a, %d %b %Y %I:%M %p %Z"),
            slot_options=options,
        )

    @staticmethod
    def _overlaps_any(slot: CandidateSlot, intervals: list[CalendarBusyInterval]) -> bool:
        """Return True when slot intersects with any busy interval."""

        for item in intervals:
            if slot.start_at < item.end_at and slot.end_at > item.start_at:
                return True
        return False

    @staticmethod
    def _collect_business_days(*, start_date, count: int):
        """Collect N business days from start_date inclusive."""

        days = []
        cursor = start_date
        while len(days) < max(0, count):
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor = cursor + timedelta(days=1)
        return days

    @staticmethod
    def _ceil_to_slot_step(value: datetime, step_minutes: int) -> datetime:
        """Round datetime upward to nearest step boundary."""

        step = max(1, step_minutes)
        minute_bucket = (value.minute // step) * step
        rounded = value.replace(minute=minute_bucket, second=0, microsecond=0)
        if rounded < value:
            rounded = rounded + timedelta(minutes=step)
        return rounded

    def _delegated_user_for_manager(self, manager_email: str) -> str | None:
        """Return delegated user identity when domain-wide delegation is enabled."""

        if self._config.use_domain_wide_delegation:
            return manager_email
        return None
