"""Interview scheduling orchestration for shortlisted candidates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.error import ApplicationValidationError
from app.core.runtime_config import SchedulingRuntimeConfig, SecurityRuntimeConfig
from app.core.security import create_interview_action_token, create_interview_confirmation_token
from app.infra.google_calendar_client import (
    CalendarBusyInterval,
    CalendarHoldEvent,
    GoogleCalendarApiError,
    GoogleCalendarClient,
)
from app.repositories.application_repository import ApplicationRepository
from app.repositories.job_opening_repository import JobOpeningRepository
from app.services.email_sender import (
    EmailSendError,
    EmailSender,
    InterviewBookingConfirmedEmail,
    InterviewRescheduleOptionsEmail,
    InterviewSlotOptionsEmail,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateSlot:
    """One interview slot candidate in UTC."""

    start_at: datetime
    end_at: datetime


class InterviewSchedulingService:
    """Generate interview options, create tentative holds, and notify candidate."""
    _CONFIRMING_STALE_AFTER_SECONDS = 120

    def __init__(
        self,
        *,
        application_repository: ApplicationRepository,
        job_opening_repository: JobOpeningRepository,
        calendar_client: GoogleCalendarClient,
        email_sender: EmailSender,
        config: SchedulingRuntimeConfig,
        security_config: SecurityRuntimeConfig,
        confirmation_token_secret: str | None,
    ) -> None:
        """Initialize scheduling dependencies."""

        self._application_repository = application_repository
        self._job_opening_repository = job_opening_repository
        self._calendar_client = calendar_client
        self._email_sender = email_sender
        self._config = config
        self._security_config = security_config
        self._confirmation_token_secret = (
            confirmation_token_secret.strip()
            if isinstance(confirmation_token_secret, str)
            else None
        )

    async def create_options_for_candidate(self, *, application_id: UUID) -> dict:
        """Create and email 3-5 held interview options for one shortlisted candidate."""

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            raise ApplicationValidationError("candidate application not found")
        if candidate.applicant_status not in set(self._config.target_statuses):
            raise ApplicationValidationError(
                f"candidate status {candidate.applicant_status} is not eligible for scheduling"
            )
        now_utc = datetime.now(tz=timezone.utc)
        if (
            candidate.interview_schedule_status in set(self._config.confirmable_statuses)
            and isinstance(candidate.interview_schedule_options, dict)
            and candidate.interview_schedule_options
        ):
            existing_expiry = self._extract_hold_expiry(
                candidate_interview_payload=candidate.interview_schedule_options
            )
            if existing_expiry is not None and existing_expiry > now_utc:
                return candidate.interview_schedule_options
            await self.expire_candidate_holds(application_id=application_id, force=True)

        opening = await self._job_opening_repository.get(candidate.job_opening_id)
        if opening is None:
            raise ApplicationValidationError("job opening not found for candidate")
        manager_email = opening.manager_email.strip().lower()
        if "@" not in manager_email:
            raise ApplicationValidationError("job opening manager_email is missing or invalid")
        delegated_user = self._delegated_user_for_manager(manager_email)

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
            application_id=application_id,
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

    async def confirm_candidate_slot(
        self,
        *,
        application_id: UUID,
        candidate_email: str,
        option_number: int,
    ) -> dict:
        """Confirm one offered slot, invite candidate, and release remaining holds."""
        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            raise ApplicationValidationError("candidate application not found")
        normalized_candidate_email = candidate_email.strip().casefold()
        if str(candidate.email).strip().casefold() != normalized_candidate_email:
            raise ApplicationValidationError("candidate email does not match application")
        previous_status = candidate.interview_schedule_status
        candidate = await self._recover_confirming_candidate_if_stale(
            application_id=application_id,
            candidate=candidate,
            requested_option_number=option_number,
        )
        previous_status = candidate.interview_schedule_status
        if previous_status not in set(self._config.confirmable_statuses):
            raise ApplicationValidationError(
                f"candidate interview status {candidate.interview_schedule_status} is not confirmable"
            )

        lock_acquired = await self._application_repository.transition_interview_schedule_status(
            application_id=application_id,
            from_statuses=set(self._config.confirmable_statuses),
            to_status="interview_confirming",
        )
        if not lock_acquired:
            raise ApplicationValidationError(
                "selected slot is being confirmed by another request; please refresh status"
            )

        try:
            payload = candidate.interview_schedule_options
            if not isinstance(payload, dict):
                raise ApplicationValidationError("interview options are not available to confirm")

            options = payload.get("options")
            if not isinstance(options, list) or not options:
                raise ApplicationValidationError("interview options are not available to confirm")

            selected_option = self._find_option_by_number(
                options=options, option_number=option_number
            )
            if selected_option is None:
                raise ApplicationValidationError("selected interview option was not found")

            now_utc = datetime.now(tz=timezone.utc)
            payload = dict(payload)
            payload["confirming_started_at"] = now_utc.isoformat()
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates={"interview_schedule_options": payload},
            )
            hold_expires_at = self._extract_hold_expiry(candidate_interview_payload=payload)
            if (
                self._config.require_confirmation_before_expiry
                and hold_expires_at is not None
                and now_utc > hold_expires_at
            ):
                await self.expire_candidate_holds(application_id=application_id, force=True)
                raise ApplicationValidationError(
                    "interview options expired; request fresh scheduling"
                )

            manager_email = self._extract_manager_email(
                candidate_interview_payload=payload, candidate=candidate
            )
            delegated_user = self._delegated_user_for_manager(manager_email)
            hold_event_id = selected_option.get("hold_event_id")
            if not isinstance(hold_event_id, str) or not hold_event_id.strip():
                raise ApplicationValidationError(
                    "selected interview option has invalid hold event id"
                )

            title = self._config.confirmed_event_title_template.format(
                candidate_name=candidate.full_name,
                role_title=candidate.role_selection,
            )
            description = self._config.confirmed_event_description_template.format(
                application_id=str(application_id),
                candidate_name=candidate.full_name,
                role_title=candidate.role_selection,
                candidate_email=str(candidate.email),
                selected_option_number=option_number,
            )
            confirmed_event = await self._confirm_hold_event_with_attendee_fallback(
                application_id=application_id,
                manager_email=manager_email,
                delegated_user=delegated_user,
                event_id=hold_event_id,
                title=title,
                description=description,
                attendee_emails=[str(candidate.email), manager_email],
                send_updates=self._config.calendar_send_updates_mode,
                extended_private_properties={
                    "hireme_application_id": str(application_id),
                    "hireme_slot_state": "confirmed",
                    "hireme_selected_option_number": str(option_number),
                },
            )

            released_event_ids = await self._release_unselected_holds(
                manager_email=manager_email,
                delegated_user=delegated_user,
                options=options,
                selected_event_id=hold_event_id,
            )

            updated_payload = dict(payload)
            updated_payload["selected_option_number"] = option_number
            updated_payload["confirmed_at"] = now_utc.isoformat()
            updated_payload["confirmed_event_id"] = confirmed_event.event_id
            updated_payload["confirmed_event_link"] = confirmed_event.html_link
            updated_payload["confirmed_meeting_link"] = confirmed_event.meeting_link
            updated_payload["released_hold_event_ids"] = released_event_ids

            updates: dict[str, Any] = {
                "interview_schedule_status": self._config.booked_status,
                "interview_schedule_options": updated_payload,
                "interview_schedule_error": None,
            }
            if self._config.move_candidate_to_in_interview_on_booking:
                updates["applicant_status"] = self._config.candidate_status_on_booking
                updates["note"] = f"candidate confirmed interview slot option {option_number}"
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates=updates,
            )

            tz = ZoneInfo(self._config.timezone)
            slot_label = (
                f"{confirmed_event.start_at.astimezone(tz):%a, %d %b %Y %I:%M %p} - "
                f"{confirmed_event.end_at.astimezone(tz):%I:%M %p} ({self._config.timezone})"
            )
            try:
                action_expires_at = now_utc + timedelta(hours=self._config.action_link_expiry_hours)
                candidate_reschedule_link = self._build_interview_action_link(
                    application_id=application_id,
                    actor="candidate",
                    action="request_reschedule",
                    expires_at=action_expires_at,
                    candidate_email=str(candidate.email),
                )
                manager_reschedule_link = self._build_interview_action_link(
                    application_id=application_id,
                    actor="manager",
                    action="request_reschedule",
                    expires_at=action_expires_at,
                )
                candidate_action_links = [
                    ("Reschedule", candidate_reschedule_link or ""),
                    ("Reject selected time", candidate_reschedule_link or ""),
                ]
                manager_action_links = [
                    ("Reschedule", manager_reschedule_link or ""),
                    ("Reject selected time", manager_reschedule_link or ""),
                ]
                await self._email_sender.send_interview_booking_confirmed(
                    InterviewBookingConfirmedEmail(
                        recipient_name=candidate.full_name,
                        recipient_email=str(candidate.email),
                        role_title=candidate.role_selection,
                        confirmed_slot=slot_label,
                        action_links=candidate_action_links,
                    )
                )
                await self._email_sender.send_interview_booking_confirmed(
                    InterviewBookingConfirmedEmail(
                        recipient_name="Hiring Manager",
                        recipient_email=manager_email,
                        role_title=candidate.role_selection,
                        confirmed_slot=slot_label,
                        action_links=manager_action_links,
                    )
                )
            except (EmailSendError, Exception) as exc:
                await self._application_repository.update_admin_review(
                    application_id=application_id,
                    updates={"interview_schedule_error": str(exc)[:1000]},
                )
                logger.exception(
                    "failed to send interview booking confirmation email application_id=%s",
                    application_id,
                )

            logger.info(
                "interview slot confirmed application_id=%s option=%s manager=%s",
                application_id,
                option_number,
                manager_email,
            )
            return updated_payload
        except Exception as exc:
            latest = await self._application_repository.get_by_id(application_id)
            if (
                previous_status in set(self._config.confirmable_statuses)
                and latest is not None
                and latest.interview_schedule_status == "interview_confirming"
            ):
                await self._application_repository.update_admin_review(
                    application_id=application_id,
                    updates={
                        "interview_schedule_status": previous_status,
                        "interview_schedule_error": str(exc)[:1000],
                    },
                )
            raise

    async def _recover_confirming_candidate_if_stale(
        self,
        *,
        application_id: UUID,
        candidate,
        requested_option_number: int,
    ):
        """Recover stale confirming state caused by interrupted confirmation attempts."""

        status = candidate.interview_schedule_status
        if status != "interview_confirming":
            return candidate

        payload = (
            candidate.interview_schedule_options
            if isinstance(candidate.interview_schedule_options, dict)
            else {}
        )
        confirmed_event_id = payload.get("confirmed_event_id")
        selected_option_number = payload.get("selected_option_number")
        if (
            isinstance(confirmed_event_id, str)
            and confirmed_event_id.strip()
            and isinstance(selected_option_number, int)
            and selected_option_number == requested_option_number
        ):
            return candidate

        now_utc = datetime.now(tz=timezone.utc)
        hold_expires_at = self._extract_hold_expiry(candidate_interview_payload=payload)
        if (
            self._config.require_confirmation_before_expiry
            and hold_expires_at is not None
            and now_utc > hold_expires_at
        ):
            await self.expire_candidate_holds(application_id=application_id, force=True)
            refreshed = await self._application_repository.get_by_id(application_id)
            if refreshed is not None:
                return refreshed
            return candidate

        confirming_started_at = self._parse_datetime_safe(
            str(payload.get("confirming_started_at"))
        )
        if confirming_started_at and (
            now_utc - confirming_started_at
        ).total_seconds() < self._CONFIRMING_STALE_AFTER_SECONDS:
            raise ApplicationValidationError(
                "selected slot is being confirmed by another request; please refresh status"
            )

        reset_status = (
            self._config.confirmable_statuses[0]
            if self._config.confirmable_statuses
            else "interview_options_sent"
        )
        await self._application_repository.update_admin_review(
            application_id=application_id,
            updates={
                "interview_schedule_status": reset_status,
                "interview_schedule_error": "stale interview_confirming state recovered",
            },
        )
        refreshed = await self._application_repository.get_by_id(application_id)
        if refreshed is None:
            raise ApplicationValidationError("candidate application not found")
        return refreshed

    async def expire_candidate_holds(
        self,
        *,
        application_id: UUID,
        force: bool = False,
    ) -> bool:
        """Release expired unconfirmed holds and mark candidate scheduling as expired."""

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            return False
        if candidate.interview_schedule_status not in set(self._config.expiry_target_statuses):
            return False
        payload = candidate.interview_schedule_options
        if not isinstance(payload, dict):
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates={
                    "interview_schedule_status": self._config.expired_status,
                    "interview_schedule_error": None,
                },
            )
            return True

        hold_expires_at = self._extract_active_hold_expiry(
            candidate_interview_payload=payload
        )
        now_utc = datetime.now(tz=timezone.utc)
        if not force and hold_expires_at is not None and now_utc <= hold_expires_at:
            return False

        manager_email = self._extract_manager_email(
            candidate_interview_payload=payload, candidate=candidate
        )
        delegated_user = self._delegated_user_for_manager(manager_email)
        options = self._extract_active_options(candidate_interview_payload=payload)
        hold_event_ids = self._extract_hold_event_ids(options=options)

        released_event_ids: list[str] = []
        for event_id in hold_event_ids:
            try:
                await self._calendar_client.delete_event(
                    calendar_id=manager_email,
                    delegated_user=delegated_user,
                    event_id=event_id,
                )
                released_event_ids.append(event_id)
            except GoogleCalendarApiError:
                logger.exception(
                    "failed to release expired hold event event_id=%s manager=%s",
                    event_id,
                    manager_email,
                )

        updated_payload = dict(payload)
        updated_payload["expired_at"] = now_utc.isoformat()
        updated_payload["released_hold_event_ids"] = released_event_ids
        reschedule_payload = (
            dict(updated_payload.get("reschedule"))
            if isinstance(updated_payload.get("reschedule"), dict)
            else None
        )
        if reschedule_payload is not None:
            reschedule_payload["expired_at"] = now_utc.isoformat()
            reschedule_payload["released_hold_event_ids"] = released_event_ids
            updated_payload["reschedule"] = reschedule_payload
        await self._application_repository.update_admin_review(
            application_id=application_id,
            updates={
                "interview_schedule_status": self._config.expired_status,
                "interview_schedule_options": updated_payload,
                "interview_schedule_error": None,
            },
        )
        logger.info(
            "expired interview holds released application_id=%s manager=%s released=%s",
            application_id,
            manager_email,
            len(released_event_ids),
        )
        return True

    async def send_reminder_for_candidate(self, *, application_id: UUID) -> bool:
        """Send one follow-up reminder for pending interview slot confirmation."""

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            return False
        if candidate.interview_schedule_status not in set(self._config.reminder_target_statuses):
            return False

        payload = candidate.interview_schedule_options
        if not isinstance(payload, dict):
            return False
        if isinstance(payload.get("reminder_sent_at"), str):
            return False

        hold_expires_at = self._extract_hold_expiry(candidate_interview_payload=payload)
        if hold_expires_at is None:
            return False
        now_utc = datetime.now(tz=timezone.utc)
        if now_utc >= hold_expires_at:
            return False

        email_payload = self._build_email_payload_from_persisted_options(
            application_id=application_id,
            candidate_name=candidate.full_name,
            candidate_email=str(candidate.email),
            role_title=candidate.role_selection,
            hold_expires_at=hold_expires_at,
            options_payload=payload,
        )
        if email_payload is None:
            return False

        await self._email_sender.send_interview_slot_reminder(email_payload)
        updated_payload = dict(payload)
        updated_payload["reminder_sent_at"] = now_utc.isoformat()
        await self._application_repository.update_admin_review(
            application_id=application_id,
            updates={
                "interview_schedule_options": updated_payload,
                "interview_schedule_error": None,
            },
        )
        logger.info(
            "interview reminder sent application_id=%s hold_expires_at=%s",
            application_id,
            hold_expires_at.isoformat(),
        )
        return True

    async def request_reschedule(
        self,
        *,
        application_id: UUID,
        actor: str,
        candidate_email: str | None = None,
    ) -> dict[str, Any]:
        """Handle candidate/manager reschedule request and send alternatives to manager."""

        normalized_actor = actor.strip().lower()
        if normalized_actor not in {"candidate", "manager"}:
            raise ApplicationValidationError("unsupported reschedule actor")

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            raise ApplicationValidationError("candidate application not found")
        allowed_statuses = {
            self._config.booked_status,
            self._config.reschedule_requested_status,
        }
        if candidate.interview_schedule_status not in allowed_statuses:
            raise ApplicationValidationError(
                f"candidate interview status {candidate.interview_schedule_status} cannot be rescheduled"
            )
        if normalized_actor == "candidate":
            expected_email = str(candidate.email).strip().casefold()
            if expected_email != (candidate_email or "").strip().casefold():
                raise ApplicationValidationError("candidate email does not match application")

        payload = (
            dict(candidate.interview_schedule_options)
            if isinstance(candidate.interview_schedule_options, dict)
            else {}
        )
        manager_email = self._extract_manager_email(
            candidate_interview_payload=payload, candidate=candidate
        )
        delegated_user = self._delegated_user_for_manager(manager_email)
        confirmed_event_id = payload.get("confirmed_event_id")
        if candidate.interview_schedule_status == self._config.booked_status:
            if isinstance(confirmed_event_id, str) and confirmed_event_id.strip():
                try:
                    await self._calendar_client.delete_event(
                        calendar_id=manager_email,
                        delegated_user=delegated_user,
                        event_id=confirmed_event_id,
                    )
                except GoogleCalendarApiError:
                    logger.exception(
                        "failed to delete previously confirmed event during reschedule application_id=%s",
                        application_id,
                    )

        reschedule = payload.get("reschedule") if isinstance(payload.get("reschedule"), dict) else {}
        current_round = int(reschedule.get("round") or 0)
        if current_round >= self._config.max_reschedule_rounds:
            raise ApplicationValidationError("maximum reschedule rounds reached")
        excluded_slot_starts = {
            value
            for value in (reschedule.get("excluded_slot_starts") or [])
            if isinstance(value, str) and value.strip()
        }
        generated = await self._generate_reschedule_options(
            application_id=application_id,
            candidate=candidate,
            manager_email=manager_email,
            delegated_user=delegated_user,
            round_number=current_round + 1,
            excluded_slot_starts=excluded_slot_starts,
        )

        manager_email_payload = self._build_manager_reschedule_email_payload(
            candidate_name=candidate.full_name,
            manager_email=manager_email,
            role_title=candidate.role_selection,
            hold_expires_at=generated["hold_expires_at"],
            options=generated["options"],
            reject_link=generated["reject_link"],
        )
        try:
            await self._email_sender.send_interview_reschedule_options_to_manager(
                manager_email_payload
            )
        except (EmailSendError, Exception):
            await self._cleanup_holds(
                manager_email=manager_email,
                delegated_user=delegated_user,
                hold_events=generated["hold_events"],
            )
            raise

        now_utc = datetime.now(tz=timezone.utc)
        updated_payload = dict(payload)
        updated_payload["reschedule"] = {
            "round": generated["round_number"],
            "requested_by": normalized_actor,
            "requested_at": now_utc.isoformat(),
            "hold_expires_at": generated["hold_expires_at"].isoformat(),
            "options": generated["options"],
            "reject_link": generated["reject_link"],
            "excluded_slot_starts": sorted(excluded_slot_starts),
            "original_confirmed_event_id": confirmed_event_id,
            "original_confirmed_event_link": payload.get("confirmed_event_link"),
            "original_confirmed_meeting_link": payload.get("confirmed_meeting_link"),
        }
        updated_payload["hold_expires_at"] = generated["hold_expires_at"].isoformat()
        await self._application_repository.update_admin_review(
            application_id=application_id,
            updates={
                "interview_schedule_status": self._config.reschedule_options_sent_status,
                "interview_schedule_options": updated_payload,
                "interview_schedule_sent_at": now_utc,
                "interview_hold_expires_at": generated["hold_expires_at"],
                "interview_calendar_email": manager_email,
                "interview_schedule_error": None,
            },
        )
        logger.info(
            "reschedule options sent application_id=%s actor=%s round=%s",
            application_id,
            normalized_actor,
            generated["round_number"],
        )
        return updated_payload

    async def process_manager_reschedule_decision(
        self,
        *,
        application_id: UUID,
        decision: str,
        round_number: int | None,
        option_number: int | None = None,
    ) -> dict[str, Any]:
        """Apply manager decision for alternative interview options."""

        normalized_decision = decision.strip().lower()
        if normalized_decision not in {"accept", "reject"}:
            raise ApplicationValidationError("unsupported manager decision")

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            raise ApplicationValidationError("candidate application not found")
        if candidate.interview_schedule_status not in set(
            self._config.manager_reschedule_confirmable_statuses
        ):
            raise ApplicationValidationError(
                f"candidate interview status {candidate.interview_schedule_status} is not manager-confirmable"
            )
        payload = (
            dict(candidate.interview_schedule_options)
            if isinstance(candidate.interview_schedule_options, dict)
            else {}
        )
        reschedule = payload.get("reschedule") if isinstance(payload.get("reschedule"), dict) else {}
        if not reschedule:
            raise ApplicationValidationError("reschedule options are not available")
        active_round = int(reschedule.get("round") or 0)
        if active_round <= 0:
            raise ApplicationValidationError("invalid reschedule round")
        if round_number is not None and round_number != active_round:
            raise ApplicationValidationError("reschedule options are outdated; request latest options")

        options = reschedule.get("options")
        if not isinstance(options, list) or not options:
            raise ApplicationValidationError("reschedule options are not available")
        manager_email = self._extract_manager_email(
            candidate_interview_payload=payload, candidate=candidate
        )
        delegated_user = self._delegated_user_for_manager(manager_email)

        if normalized_decision == "reject":
            released_ids = await self._release_unselected_holds(
                manager_email=manager_email,
                delegated_user=delegated_user,
                options=options,
                selected_event_id="__none__",
            )
            excluded_slot_starts = {
                value
                for value in (reschedule.get("excluded_slot_starts") or [])
                if isinstance(value, str) and value.strip()
            }
            for item in options:
                if isinstance(item, dict):
                    start_at = item.get("start_at")
                    if isinstance(start_at, str) and start_at.strip():
                        excluded_slot_starts.add(start_at)
            reschedule["rejected_at"] = datetime.now(tz=timezone.utc).isoformat()
            reschedule["released_hold_event_ids"] = released_ids
            reschedule["excluded_slot_starts"] = sorted(excluded_slot_starts)
            payload["reschedule"] = reschedule
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates={
                    "interview_schedule_options": payload,
                    "interview_schedule_status": self._config.reschedule_requested_status,
                    "interview_schedule_error": None,
                },
            )
            return await self.request_reschedule(
                application_id=application_id,
                actor="manager",
            )

        if not isinstance(option_number, int) or option_number <= 0:
            raise ApplicationValidationError("manager accept action missing option number")

        lock_acquired = await self._application_repository.transition_interview_schedule_status(
            application_id=application_id,
            from_statuses=set(self._config.manager_reschedule_confirmable_statuses),
            to_status=self._config.reschedule_confirming_status,
        )
        if not lock_acquired:
            raise ApplicationValidationError("selected slot is being confirmed; refresh status")

        previous_status = self._config.reschedule_options_sent_status
        try:
            selected_option = self._find_option_by_number(
                options=options,
                option_number=option_number,
            )
            if selected_option is None:
                raise ApplicationValidationError("selected reschedule option was not found")
            now_utc = datetime.now(tz=timezone.utc)
            hold_expires_at = self._parse_datetime_safe(str(reschedule.get("hold_expires_at")))
            if hold_expires_at and now_utc > hold_expires_at:
                await self.expire_candidate_holds(application_id=application_id, force=True)
                raise ApplicationValidationError("reschedule options expired; request fresh options")

            hold_event_id = selected_option.get("hold_event_id")
            if not isinstance(hold_event_id, str) or not hold_event_id.strip():
                raise ApplicationValidationError("selected reschedule option has invalid hold id")

            title = self._config.confirmed_event_title_template.format(
                candidate_name=candidate.full_name,
                role_title=candidate.role_selection,
            )
            description = self._config.confirmed_event_description_template.format(
                application_id=str(application_id),
                candidate_name=candidate.full_name,
                role_title=candidate.role_selection,
                candidate_email=str(candidate.email),
                selected_option_number=option_number,
            )
            confirmed_event = await self._confirm_hold_event_with_attendee_fallback(
                application_id=application_id,
                manager_email=manager_email,
                delegated_user=delegated_user,
                event_id=hold_event_id,
                title=title,
                description=description,
                attendee_emails=[str(candidate.email), manager_email],
                send_updates=self._config.calendar_send_updates_mode,
                extended_private_properties={
                    "hireme_application_id": str(application_id),
                    "hireme_slot_state": "confirmed",
                    "hireme_reschedule_round": str(active_round),
                    "hireme_selected_option_number": str(option_number),
                },
            )
            released_event_ids = await self._release_unselected_holds(
                manager_email=manager_email,
                delegated_user=delegated_user,
                options=options,
                selected_event_id=hold_event_id,
            )
            payload["selected_option_number"] = option_number
            payload["confirmed_at"] = now_utc.isoformat()
            payload["confirmed_event_id"] = confirmed_event.event_id
            payload["confirmed_event_link"] = confirmed_event.html_link
            payload["confirmed_meeting_link"] = confirmed_event.meeting_link
            payload["released_hold_event_ids"] = released_event_ids
            payload["reschedule"] = {
                **reschedule,
                "accepted_at": now_utc.isoformat(),
                "accepted_option_number": option_number,
                "released_hold_event_ids": released_event_ids,
            }
            updates: dict[str, Any] = {
                "interview_schedule_status": self._config.booked_status,
                "interview_schedule_options": payload,
                "interview_schedule_error": None,
            }
            if self._config.move_candidate_to_in_interview_on_booking:
                updates["applicant_status"] = self._config.candidate_status_on_booking
                updates["note"] = (
                    f"manager accepted reschedule option {option_number} in round {active_round}"
                )
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates=updates,
            )

            tz = ZoneInfo(self._config.timezone)
            slot_label = (
                f"{confirmed_event.start_at.astimezone(tz):%a, %d %b %Y %I:%M %p} - "
                f"{confirmed_event.end_at.astimezone(tz):%I:%M %p} ({self._config.timezone})"
            )
            action_expires_at = now_utc + timedelta(hours=self._config.action_link_expiry_hours)
            candidate_reschedule_link = self._build_interview_action_link(
                application_id=application_id,
                actor="candidate",
                action="request_reschedule",
                expires_at=action_expires_at,
                candidate_email=str(candidate.email),
            )
            manager_reschedule_link = self._build_interview_action_link(
                application_id=application_id,
                actor="manager",
                action="request_reschedule",
                expires_at=action_expires_at,
            )
            await self._email_sender.send_interview_booking_confirmed(
                InterviewBookingConfirmedEmail(
                    recipient_name=candidate.full_name,
                    recipient_email=str(candidate.email),
                    role_title=candidate.role_selection,
                    confirmed_slot=slot_label,
                    action_links=[
                        ("Reschedule", candidate_reschedule_link or ""),
                        ("Reject selected time", candidate_reschedule_link or ""),
                    ],
                )
            )
            await self._email_sender.send_interview_booking_confirmed(
                InterviewBookingConfirmedEmail(
                    recipient_name="Hiring Manager",
                    recipient_email=manager_email,
                    role_title=candidate.role_selection,
                    confirmed_slot=slot_label,
                    action_links=[
                        ("Reschedule", manager_reschedule_link or ""),
                        ("Reject selected time", manager_reschedule_link or ""),
                    ],
                )
            )
            return payload
        except Exception as exc:
            latest = await self._application_repository.get_by_id(application_id)
            if (
                latest is not None
                and latest.interview_schedule_status == self._config.reschedule_confirming_status
            ):
                await self._application_repository.update_admin_review(
                    application_id=application_id,
                    updates={
                        "interview_schedule_status": previous_status,
                        "interview_schedule_error": str(exc)[:1000],
                    },
                )
            raise

    async def _generate_reschedule_options(
        self,
        *,
        application_id: UUID,
        candidate,
        manager_email: str,
        delegated_user: str | None,
        round_number: int,
        excluded_slot_starts: set[str],
    ) -> dict[str, Any]:
        """Create held alternative slots and action links for manager approval."""

        now_utc = datetime.now(tz=timezone.utc)
        hold_expires_at = now_utc + timedelta(hours=self._config.hold_expiry_hours)
        window_start, window_end = self._compute_time_window(now_utc)
        busy_intervals = await self._calendar_client.list_busy_intervals(
            calendar_id=manager_email,
            time_min=window_start,
            time_max=window_end,
            delegated_user=delegated_user,
        )
        free_slots = [
            slot
            for slot in self._select_free_slots(
                now_utc=now_utc,
                busy_intervals=busy_intervals,
                max_results=(
                    self._config.max_slots
                    + len(excluded_slot_starts)
                    + self._config.min_slots
                ),
            )
            if slot.start_at.isoformat() not in excluded_slot_starts
        ]
        if len(free_slots) < self._config.min_slots:
            raise ApplicationValidationError(
                "insufficient alternative interview slots found for reschedule"
            )
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
            raise ApplicationValidationError("failed to create enough reschedule hold events")

        options_payload = self._build_options_payload(
            manager_email=manager_email,
            hold_expires_at=hold_expires_at,
            hold_events=hold_events,
        )
        options = []
        for item in options_payload.get("options", []):
            if not isinstance(item, dict):
                continue
            opt = dict(item)
            option_number = opt.get("option_number")
            if isinstance(option_number, int):
                opt["accept_link"] = self._build_interview_action_link(
                    application_id=application_id,
                    actor="manager",
                    action="manager_accept_reschedule",
                    option_number=option_number,
                    round_number=round_number,
                    expires_at=hold_expires_at,
                )
            options.append(opt)
        reject_link = self._build_interview_action_link(
            application_id=application_id,
            actor="manager",
            action="manager_reject_reschedule",
            round_number=round_number,
            expires_at=hold_expires_at,
        )
        return {
            "round_number": round_number,
            "hold_expires_at": hold_expires_at,
            "options": options,
            "reject_link": reject_link,
            "hold_events": hold_events,
        }

    def _build_manager_reschedule_email_payload(
        self,
        *,
        candidate_name: str,
        manager_email: str,
        role_title: str,
        hold_expires_at: datetime,
        options: list[dict[str, Any]],
        reject_link: str | None,
    ) -> InterviewRescheduleOptionsEmail:
        """Build manager-facing reschedule options email with accept/reject links."""

        tz = ZoneInfo(self._config.timezone)
        lines: list[str] = []
        links: list[tuple[str, str]] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            option_number = option.get("option_number")
            start_at = self._parse_datetime_safe(str(option.get("start_at")))
            end_at = self._parse_datetime_safe(str(option.get("end_at")))
            if not isinstance(option_number, int) or start_at is None or end_at is None:
                continue
            label = (
                f"Option {option_number}: {start_at.astimezone(tz):%a, %d %b %Y %I:%M %p} - "
                f"{end_at.astimezone(tz):%I:%M %p} ({self._config.timezone})"
            )
            accept_link = option.get("accept_link")
            suffix = f" | Accept: {accept_link}" if isinstance(accept_link, str) else ""
            lines.append(f"{label}{suffix}")
            if isinstance(accept_link, str) and accept_link.strip():
                links.append((label, accept_link))
        return InterviewRescheduleOptionsEmail(
            candidate_name=candidate_name,
            manager_email=manager_email,
            role_title=role_title,
            hold_expires_at=hold_expires_at.astimezone(tz).strftime("%a, %d %b %Y %I:%M %p %Z"),
            slot_options=lines,
            slot_option_links=links,
            reject_link=reject_link,
        )

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
        max_results: int | None = None,
    ) -> list[CandidateSlot]:
        """Generate and filter free candidate slots, capped to max_slots."""

        tz = ZoneInfo(self._config.timezone)
        min_start_local = now_utc.astimezone(tz) + timedelta(hours=self._config.min_notice_hours)
        duration = timedelta(minutes=self._config.slot_duration_minutes)
        step = timedelta(minutes=self._config.slot_step_minutes)
        result_cap = (
            max(1, int(max_results))
            if isinstance(max_results, int) and max_results > 0
            else self._config.max_slots
        )

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
                    if len(candidates) >= result_cap:
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
        application_id: UUID,
        candidate_name: str,
        candidate_email: str,
        role_title: str,
        hold_events: list[CalendarHoldEvent],
        hold_expires_at: datetime,
    ) -> InterviewSlotOptionsEmail:
        """Render email payload with numbered human-readable options."""

        tz = ZoneInfo(self._config.timezone)
        options: list[str] = []
        option_links: list[tuple[str, str]] = []
        for index, event in enumerate(hold_events, start=1):
            start_local = event.start_at.astimezone(tz)
            end_local = event.end_at.astimezone(tz)
            confirmation_link = self._build_confirmation_link(
                application_id=application_id,
                candidate_email=candidate_email,
                option_number=index,
                expires_at=hold_expires_at,
            )
            suffix = f" | Click to confirm: {confirmation_link}" if confirmation_link else ""
            option_label = (
                f"Option {index}: {start_local:%a, %d %b %Y %I:%M %p} - "
                f"{end_local:%I:%M %p} ({self._config.timezone})"
            )
            options.append(
                f"{option_label}{suffix}"
            )
            if confirmation_link:
                option_links.append((option_label, confirmation_link))

        return InterviewSlotOptionsEmail(
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            role_title=role_title,
            hold_expires_at=hold_expires_at.astimezone(tz).strftime("%a, %d %b %Y %I:%M %p %Z"),
            slot_options=options,
            slot_option_links=option_links,
        )

    def _build_email_payload_from_persisted_options(
        self,
        *,
        application_id: UUID,
        candidate_name: str,
        candidate_email: str,
        role_title: str,
        hold_expires_at: datetime,
        options_payload: dict[str, Any],
    ) -> InterviewSlotOptionsEmail | None:
        """Render interview option email payload from stored options JSON."""

        options_raw = options_payload.get("options")
        if not isinstance(options_raw, list) or not options_raw:
            return None

        tz = ZoneInfo(self._config.timezone)
        option_lines: list[str] = []
        option_links: list[tuple[str, str]] = []
        for item in options_raw:
            if not isinstance(item, dict):
                continue
            option_number = item.get("option_number")
            raw_start = item.get("start_at")
            raw_end = item.get("end_at")
            if not isinstance(option_number, int):
                continue
            if not isinstance(raw_start, str) or not isinstance(raw_end, str):
                continue

            start_at = self._parse_datetime_safe(raw_start)
            end_at = self._parse_datetime_safe(raw_end)
            if start_at is None or end_at is None:
                continue

            confirmation_link = self._build_confirmation_link(
                application_id=application_id,
                candidate_email=candidate_email,
                option_number=option_number,
                expires_at=hold_expires_at,
            )
            option_label = (
                f"Option {option_number}: {start_at.astimezone(tz):%a, %d %b %Y %I:%M %p} - "
                f"{end_at.astimezone(tz):%I:%M %p} ({self._config.timezone})"
            )
            suffix = f" | Click to confirm: {confirmation_link}" if confirmation_link else ""
            option_lines.append(f"{option_label}{suffix}")
            if confirmation_link:
                option_links.append((option_label, confirmation_link))

        if not option_lines:
            return None

        return InterviewSlotOptionsEmail(
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            role_title=role_title,
            hold_expires_at=hold_expires_at.astimezone(tz).strftime("%a, %d %b %Y %I:%M %p %Z"),
            slot_options=option_lines,
            slot_option_links=option_links,
        )

    def _build_confirmation_link(
        self,
        *,
        application_id: UUID,
        candidate_email: str,
        option_number: int,
        expires_at: datetime,
    ) -> str | None:
        """Build tokenized frontend confirmation link for one interview option."""

        if not self._confirmation_token_secret:
            logger.warning("missing confirmation token secret; option links will be omitted")
            return None
        token = create_interview_confirmation_token(
            application_id=application_id,
            candidate_email=candidate_email,
            option_number=option_number,
            expires_at=expires_at,
            secret=self._confirmation_token_secret,
            config=self._security_config,
        )
        base_url = self._config.candidate_confirmation_page_url.rstrip("/")
        encoded_token = quote(token, safe="")
        return f"{base_url}?token={encoded_token}"

    def _build_interview_action_link(
        self,
        *,
        application_id: UUID,
        actor: str,
        action: str,
        expires_at: datetime,
        option_number: int | None = None,
        round_number: int | None = None,
        candidate_email: str | None = None,
    ) -> str | None:
        """Build tokenized interview action link (reschedule/reject/accept)."""

        if not self._confirmation_token_secret:
            logger.warning("missing confirmation token secret; interview action links omitted")
            return None
        token = create_interview_action_token(
            application_id=application_id,
            actor=actor,
            action=action,
            option_number=option_number,
            round_number=round_number,
            candidate_email=candidate_email,
            expires_at=expires_at,
            secret=self._confirmation_token_secret,
            config=self._security_config,
        )
        if actor == "manager":
            base_url = self._config.manager_reschedule_action_page_url.rstrip("/")
        else:
            base_url = self._config.interview_action_page_url.rstrip("/")
        return f"{base_url}?token={quote(token, safe='')}"

    async def _confirm_hold_event_with_attendee_fallback(
        self,
        *,
        application_id: UUID,
        manager_email: str,
        delegated_user: str | None,
        event_id: str,
        title: str,
        description: str,
        attendee_emails: list[str],
        send_updates: str,
        extended_private_properties: dict[str, str],
    ) -> CalendarHoldEvent:
        """Confirm hold event and retry without attendees for service-account restrictions."""

        try:
            return await self._calendar_client.confirm_hold_event(
                calendar_id=manager_email,
                delegated_user=delegated_user,
                event_id=event_id,
                title=title,
                description=description,
                attendee_emails=attendee_emails,
                send_updates=send_updates,
                extended_private_properties=extended_private_properties,
            )
        except GoogleCalendarApiError as exc:
            error_text = f"{exc} {exc.__cause__ or ''}".lower()
            is_invite_restriction = (
                "forbiddenforserviceaccounts" in error_text
                or "service account" in error_text
                or "invite attendees" in error_text
            )
            if not is_invite_restriction:
                raise
            logger.warning(
                "service-account attendee invite blocked; retrying without attendees "
                "application_id=%s manager_email=%s event_id=%s",
                application_id,
                manager_email,
                event_id,
            )
            return await self._calendar_client.confirm_hold_event(
                calendar_id=manager_email,
                delegated_user=delegated_user,
                event_id=event_id,
                title=title,
                description=description,
                attendee_emails=[],
                send_updates="none",
                extended_private_properties=extended_private_properties,
            )

    async def _release_unselected_holds(
        self,
        *,
        manager_email: str,
        delegated_user: str | None,
        options: list[Any],
        selected_event_id: str,
    ) -> list[str]:
        """Delete non-selected hold events after candidate confirmation."""

        if not self._config.release_other_holds_on_confirm:
            return []

        released_event_ids: list[str] = []
        for item in options:
            if not isinstance(item, dict):
                continue
            hold_event_id = item.get("hold_event_id")
            if not isinstance(hold_event_id, str) or not hold_event_id.strip():
                continue
            if hold_event_id == selected_event_id:
                continue
            try:
                await self._calendar_client.delete_event(
                    calendar_id=manager_email,
                    delegated_user=delegated_user,
                    event_id=hold_event_id,
                )
                released_event_ids.append(hold_event_id)
            except GoogleCalendarApiError:
                logger.exception(
                    "failed to release unselected hold event event_id=%s manager=%s",
                    hold_event_id,
                    manager_email,
                )
        return released_event_ids

    @staticmethod
    def _find_option_by_number(
        *,
        options: list[Any],
        option_number: int,
    ) -> dict[str, Any] | None:
        """Find one option row by numeric option_number field."""

        for item in options:
            if not isinstance(item, dict):
                continue
            raw_option_number = item.get("option_number")
            if isinstance(raw_option_number, int) and raw_option_number == option_number:
                return item
        return None

    @staticmethod
    def _extract_hold_event_ids(*, options: Any) -> list[str]:
        """Extract hold event ids from persisted options payload."""

        if not isinstance(options, list):
            return []
        event_ids: list[str] = []
        for item in options:
            if not isinstance(item, dict):
                continue
            hold_event_id = item.get("hold_event_id")
            if isinstance(hold_event_id, str) and hold_event_id.strip():
                event_ids.append(hold_event_id)
        return event_ids

    @staticmethod
    def _extract_hold_expiry(*, candidate_interview_payload: dict[str, Any]) -> datetime | None:
        """Extract hold expiry timestamp from interview options payload."""

        raw_value = candidate_interview_payload.get("hold_expires_at")
        if not isinstance(raw_value, str) or not raw_value.strip():
            return None
        return InterviewSchedulingService._parse_datetime_safe(raw_value)

    @staticmethod
    def _extract_active_hold_expiry(
        *, candidate_interview_payload: dict[str, Any]
    ) -> datetime | None:
        """Extract active hold expiry from top-level or reschedule payload."""

        reschedule = candidate_interview_payload.get("reschedule")
        if isinstance(reschedule, dict):
            raw_reschedule_expiry = reschedule.get("hold_expires_at")
            if isinstance(raw_reschedule_expiry, str) and raw_reschedule_expiry.strip():
                parsed = InterviewSchedulingService._parse_datetime_safe(raw_reschedule_expiry)
                if parsed is not None:
                    return parsed
        return InterviewSchedulingService._extract_hold_expiry(
            candidate_interview_payload=candidate_interview_payload
        )

    @staticmethod
    def _extract_active_options(*, candidate_interview_payload: dict[str, Any]) -> Any:
        """Extract active option list from top-level or reschedule payload."""

        reschedule = candidate_interview_payload.get("reschedule")
        if isinstance(reschedule, dict):
            options = reschedule.get("options")
            if isinstance(options, list):
                return options
        return candidate_interview_payload.get("options")

    @staticmethod
    def _parse_datetime_safe(value: str) -> datetime | None:
        """Parse iso datetime safely and normalize to UTC timezone."""

        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _extract_manager_email(
        *,
        candidate_interview_payload: dict[str, Any],
        candidate,
    ) -> str:
        """Resolve manager calendar email from payload or candidate record."""

        raw_manager = candidate_interview_payload.get("manager_email")
        if isinstance(raw_manager, str) and "@" in raw_manager:
            return raw_manager.strip().lower()
        calendar_email = candidate.interview_calendar_email
        if isinstance(calendar_email, str) and "@" in calendar_email:
            return calendar_email.strip().lower()
        raise ApplicationValidationError("manager calendar email missing for interview options")

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
