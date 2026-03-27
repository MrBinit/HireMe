"""Background worker to release expired interview hold events."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_email_sender
from app.core.runtime_config import SchedulingRuntimeConfig, get_runtime_config
from app.core.settings import get_settings
from app.infra.database import get_async_session_factory
from app.infra.google_calendar_client import GoogleCalendarClient
from app.model.applicant_application import ApplicantApplication
from app.repositories.postgres_application_repository import PostgresApplicationRepository
from app.repositories.postgres_job_opening_repository import PostgresJobOpeningRepository
from app.services.email_sender import EmailSendError, EmailSender, InterviewParticipationThanksEmail
from app.services.interview_scheduling_service import InterviewSchedulingService
from app.services.fireflies_service import FirefliesApiError, FirefliesService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class InterviewHoldExpiryWorker:
    """Poll DB for expired holds and release them from manager calendar."""

    def __init__(
        self,
        *,
        config: SchedulingRuntimeConfig,
        session_factory: async_sessionmaker[AsyncSession],
        scheduling_service: InterviewSchedulingService,
        application_repository: PostgresApplicationRepository,
        fireflies_service: FirefliesService,
        email_sender: EmailSender,
    ) -> None:
        """Initialize expiry worker dependencies."""

        self._config = config
        self._session_factory = session_factory
        self._scheduling_service = scheduling_service
        self._application_repository = application_repository
        self._fireflies_service = fireflies_service
        self._email_sender = email_sender

    async def run_forever(self) -> None:
        """Run expiry reconciliation loop forever."""

        logger.info(
            "starting interview hold expiry worker poll=%ss batch=%s statuses=%s fireflies_enabled=%s",
            self._config.expiry_poll_interval_seconds,
            self._config.expiry_batch_size,
            self._config.expiry_target_statuses,
            self._fireflies_service.enabled,
        )
        while True:
            try:
                processed_count = await self.run_once()
                if processed_count:
                    logger.info(
                        "interview scheduling maintenance cycle completed processed=%s",
                        processed_count,
                    )
            except Exception:
                logger.exception("expired hold release cycle failed")
            await asyncio.sleep(max(1, self._config.expiry_poll_interval_seconds))

    async def run_once(self) -> int:
        """Release expired holds for one polling cycle."""

        fireflies_count = await self._sync_fireflies_transcripts()
        reminder_count = await self._send_pending_reminders()
        if not self._config.auto_release_expired_holds:
            return fireflies_count + reminder_count

        now_utc = datetime.now(tz=timezone.utc)
        application_ids = await self._fetch_expired_application_ids(now_utc=now_utc)
        released = 0
        for application_id in application_ids:
            try:
                if await self._scheduling_service.expire_candidate_holds(
                    application_id=application_id
                ):
                    released += 1
            except Exception:
                logger.exception(
                    "failed to release expired holds for application_id=%s",
                    application_id,
                )
        return fireflies_count + reminder_count + released

    async def _send_pending_reminders(self) -> int:
        """Send one follow-up reminder to candidates with unconfirmed held slots."""

        if not self._config.reminder_enabled:
            return 0

        now_utc = datetime.now(tz=timezone.utc)
        threshold = now_utc - timedelta(hours=max(1, self._config.reminder_after_hours))
        application_ids = await self._fetch_reminder_application_ids(
            threshold_utc=threshold,
            now_utc=now_utc,
        )
        sent = 0
        for application_id in application_ids:
            try:
                if await self._scheduling_service.send_reminder_for_candidate(
                    application_id=application_id
                ):
                    sent += 1
            except Exception:
                logger.exception(
                    "failed to send interview reminder for application_id=%s",
                    application_id,
                )
        if sent:
            logger.info("interview reminder cycle completed sent=%s", sent)
        return sent

    async def _sync_fireflies_transcripts(self) -> int:
        """Sync transcript + summary for booked interviews via Fireflies API."""

        if not self._fireflies_service.enabled:
            return 0

        now_utc = datetime.now(tz=timezone.utc)
        application_ids = await self._fetch_fireflies_sync_application_ids()
        updated = 0
        for application_id in application_ids:
            candidate = await self._application_repository.get_by_id(application_id)
            if candidate is None:
                continue
            payload = candidate.interview_schedule_options
            if not isinstance(payload, dict) or not payload:
                continue
            manager_email = (
                payload.get("confirmed_manager_email")
                if isinstance(payload.get("confirmed_manager_email"), str)
                else payload.get("manager_email")
            )
            if not isinstance(manager_email, str):
                manager_email = candidate.interview_calendar_email
            if not self._fireflies_service.should_track_manager(manager_email):
                continue

            updated_payload = deepcopy(payload)
            changed = False
            column_updates: dict[str, Any] = {}
            fireflies_state = (
                deepcopy(updated_payload.get("fireflies"))
                if isinstance(updated_payload.get("fireflies"), dict)
                else None
            )
            confirmed_event_id = updated_payload.get("confirmed_event_id")
            confirmed_start_at, confirmed_end_at = self._extract_confirmed_window(updated_payload)
            meeting_link = (
                updated_payload.get("confirmed_meeting_link")
                if isinstance(updated_payload.get("confirmed_meeting_link"), str)
                else None
            )
            if fireflies_state is None:
                if (
                    isinstance(confirmed_event_id, str)
                    and confirmed_event_id.strip()
                    and confirmed_start_at is not None
                    and confirmed_end_at is not None
                    and isinstance(manager_email, str)
                ):
                    fireflies_state = self._fireflies_service.build_tracking_state(
                        manager_email=manager_email,
                        candidate_email=str(candidate.email),
                        meeting_link=meeting_link,
                        confirmed_event_id=confirmed_event_id,
                        confirmed_start_at=confirmed_start_at,
                        confirmed_end_at=confirmed_end_at,
                    )
                    updated_payload["fireflies"] = fireflies_state
                    column_updates.setdefault("interview_transcript_status", "pending")
                    changed = True
                else:
                    continue

            meeting_link = (
                fireflies_state.get("meeting_link")
                if isinstance(fireflies_state.get("meeting_link"), str)
                else meeting_link
            )
            if not isinstance(meeting_link, str) or not meeting_link.strip():
                fireflies_state["status"] = "skipped_missing_meeting_link"
                updated_payload["fireflies"] = fireflies_state
                column_updates.update(
                    {
                        "interview_transcript_status": "failed",
                        "interview_transcript_summary": (
                            "transcript unavailable: meeting link missing on confirmed event"
                        ),
                    }
                )
                changed = True
                await self._application_repository.update_admin_review(
                    application_id=application_id,
                    updates={"interview_schedule_options": updated_payload, **column_updates},
                )
                updated += 1
                continue

            meeting_start_at = self._parse_datetime_safe(fireflies_state.get("meeting_start_at"))
            meeting_end_at = self._parse_datetime_safe(fireflies_state.get("meeting_end_at"))
            if meeting_start_at is None or meeting_end_at is None:
                meeting_start_at = confirmed_start_at
                meeting_end_at = confirmed_end_at
                if meeting_start_at and meeting_end_at:
                    fireflies_state["meeting_start_at"] = meeting_start_at.isoformat()
                    fireflies_state["meeting_end_at"] = meeting_end_at.isoformat()
                    changed = True
            if meeting_start_at is None or meeting_end_at is None:
                continue

            bot_request = (
                deepcopy(fireflies_state.get("bot_request"))
                if isinstance(fireflies_state.get("bot_request"), dict)
                else {}
            )
            transcript_sync = (
                deepcopy(fireflies_state.get("transcript_sync"))
                if isinstance(fireflies_state.get("transcript_sync"), dict)
                else {}
            )
            fireflies_state["bot_request"] = bot_request
            fireflies_state["transcript_sync"] = transcript_sync

            sync_status = str(transcript_sync.get("status") or "pending").strip().lower()
            if self._config.fireflies.mock_mode and sync_status != "completed":
                fireflies_state["status"] = "completed"
                fireflies_state["completed_at"] = now_utc.isoformat()
                transcript_sync["status"] = "completed"
                transcript_sync["last_checked_at"] = now_utc.isoformat()
                transcript_sync["attempts"] = int(transcript_sync.get("attempts") or 0) + 1
                fireflies_state["transcript"] = {
                    "id": f"mock-{application_id}",
                    "title": f"Mock HireMe Interview - {candidate.full_name}",
                    "url": f"https://app.fireflies.ai/view/mock-{application_id}",
                    "meeting_link": meeting_link,
                    "occurred_at": now_utc.isoformat(),
                    "summary": (
                        f"Mock transcript summary for {candidate.full_name}: "
                        f"strong discussion on role {candidate.role_selection}, "
                        "clear communication, and relevant project depth."
                    ),
                    "action_items": [
                        "Send interview feedback to hiring panel.",
                        "Share next-round expectations with candidate.",
                    ],
                    "keywords": ["hireme", "technical interview", "ai engineer"],
                    "raw": {"mock": True},
                }
                column_updates.update(
                    {
                        "interview_transcript_status": "completed",
                        "interview_transcript_url": fireflies_state["transcript"]["url"],
                        "interview_transcript_summary": fireflies_state["transcript"]["summary"],
                        "interview_transcript_synced_at": now_utc,
                    }
                )
                if self._config.fireflies.update_schedule_status_on_complete:
                    column_updates["interview_schedule_status"] = (
                        self._config.fireflies.completed_schedule_status
                    )
                changed = True

            live_join_open_at = meeting_start_at - timedelta(
                minutes=max(0, self._config.fireflies.join_before_minutes)
            )
            live_join_close_at = meeting_end_at + timedelta(minutes=30)
            transcript_ready_at = meeting_end_at + timedelta(
                minutes=max(0, self._config.fireflies.transcript_poll_delay_minutes)
            )
            bot_last_attempt = self._parse_datetime_safe(bot_request.get("last_attempt_at"))
            bot_attempts = int(bot_request.get("attempts") or 0)
            bot_status = str(bot_request.get("status") or "pending").strip().lower()
            cooldown = timedelta(minutes=max(1, self._config.fireflies.join_retry_cooldown_minutes))
            can_retry_join = bot_last_attempt is None or (now_utc - bot_last_attempt) >= cooldown
            within_live_window = live_join_open_at <= now_utc <= live_join_close_at
            should_retry_requested_during_live_window = (
                bot_status == "requested"
                and within_live_window
                and sync_status in {"pending", "polling"}
            )
            allow_join_for_status = (
                bot_status in {"pending", "retry_pending"}
                or should_retry_requested_during_live_window
            )
            if (
                not self._config.fireflies.mock_mode
                and within_live_window
                and allow_join_for_status
                and can_retry_join
            ):
                bot_attempts += 1
                bot_request["attempts"] = bot_attempts
                bot_request["last_attempt_at"] = now_utc.isoformat()
                try:
                    join_result = await self._fireflies_service.request_live_capture(
                        meeting_link=meeting_link,
                        title=f"HireMe interview - {candidate.full_name}",
                    )
                    if bool(join_result.get("success")):
                        bot_request["status"] = "requested"
                        bot_request["last_error"] = None
                    else:
                        bot_request["status"] = "retry_pending"
                        bot_request["last_error"] = (
                            str(join_result.get("error") or join_result.get("message"))
                        )[:500]
                    changed = True
                except FirefliesApiError as exc:
                    bot_request["status"] = "retry_pending"
                    bot_request["last_error"] = str(exc)[:500]
                    changed = True

            sync_attempts = int(transcript_sync.get("attempts") or 0)
            last_checked_at = self._parse_datetime_safe(transcript_sync.get("last_checked_at"))
            sync_interval = timedelta(
                minutes=max(1, self._config.fireflies.transcript_poll_interval_minutes)
            )
            max_poll_attempts = max(1, self._config.fireflies.max_poll_attempts)
            should_poll = (
                not self._config.fireflies.mock_mode
                and now_utc >= transcript_ready_at
                and sync_status not in {"completed", "not_found"}
                and sync_attempts < max_poll_attempts
                and (last_checked_at is None or (now_utc - last_checked_at) >= sync_interval)
            )
            if should_poll:
                sync_attempts += 1
                transcript_sync["attempts"] = sync_attempts
                transcript_sync["last_checked_at"] = now_utc.isoformat()
                transcript_sync["status"] = "polling"
                try:
                    match = await self._fireflies_service.find_best_transcript(
                        manager_email=manager_email,
                        candidate_email=str(candidate.email),
                        meeting_link=meeting_link,
                        candidate_name=candidate.full_name,
                        meeting_start_at=meeting_start_at,
                    )
                except FirefliesApiError as exc:
                    transcript_sync["status"] = "polling"
                    transcript_sync["last_error"] = str(exc)[:500]
                    fireflies_state["status"] = "processing"
                    column_updates["interview_transcript_status"] = "processing"
                    changed = True
                else:
                    if match is not None:
                        resolved_transcript_url = (
                            match.transcript_url.strip()
                            if isinstance(match.transcript_url, str)
                            else ""
                        )
                        resolved_summary = (
                            match.summary_text.strip()
                            if isinstance(match.summary_text, str)
                            else ""
                        )
                        resolved_action_items = list(match.action_items)
                        resolved_keywords = list(match.keywords)
                        resolved_raw = deepcopy(match.raw)
                        if resolved_transcript_url or resolved_summary or resolved_action_items:
                            transcript_sync["status"] = "completed"
                            transcript_sync["last_error"] = None
                            fireflies_state["status"] = "completed"
                            fireflies_state["transcript"] = {
                                "id": match.transcript_id,
                                "title": match.title,
                                "url": resolved_transcript_url or None,
                                "video_url": match.video_url,
                                "meeting_link": match.meeting_link,
                                "occurred_at": (
                                    match.occurred_at.isoformat() if match.occurred_at else None
                                ),
                                "summary": resolved_summary or None,
                                "action_items": resolved_action_items,
                                "keywords": resolved_keywords,
                                "raw": resolved_raw,
                            }
                            fireflies_state["completed_at"] = now_utc.isoformat()
                            column_updates.update(
                                {
                                    "interview_transcript_status": "completed",
                                    "interview_transcript_url": resolved_transcript_url or None,
                                    "interview_transcript_summary": resolved_summary or None,
                                    "interview_transcript_synced_at": now_utc,
                                }
                            )
                            if self._config.fireflies.update_schedule_status_on_complete:
                                column_updates["interview_schedule_status"] = (
                                    self._config.fireflies.completed_schedule_status
                                )
                        elif sync_attempts >= max_poll_attempts:
                            transcript_sync["status"] = "not_found"
                            transcript_sync["last_error"] = (
                                "matched Fireflies transcript has no transcript URL, summary, "
                                "or action items"
                            )
                            fireflies_state["status"] = "not_found"
                            column_updates["interview_transcript_status"] = "not_found"
                            column_updates["interview_transcript_summary"] = (
                                "transcript matched in Fireflies but no transcript content was available"
                            )
                        else:
                            transcript_sync["status"] = "polling"
                            transcript_sync["last_error"] = (
                                "matched Fireflies transcript has no transcript content yet"
                            )
                            fireflies_state["status"] = "processing"
                            column_updates["interview_transcript_status"] = "processing"
                    elif sync_attempts >= max_poll_attempts:
                        transcript_sync["status"] = "not_found"
                        fireflies_state["status"] = "not_found"
                        column_updates["interview_transcript_status"] = "not_found"
                        column_updates["interview_transcript_summary"] = (
                            "transcript not found in Fireflies within polling window"
                        )
                    else:
                        transcript_sync["status"] = "polling"
                        fireflies_state["status"] = "processing"
                        column_updates["interview_transcript_status"] = "processing"
                    changed = True

            if str(fireflies_state.get("status") or "").strip().lower() == "completed":
                if await self._maybe_send_interview_thank_you_email(
                    candidate=candidate,
                    fireflies_state=fireflies_state,
                    now_utc=now_utc,
                ):
                    changed = True

            if changed:
                updated_payload["fireflies"] = fireflies_state
                if "interview_transcript_status" not in column_updates and fireflies_state.get(
                    "status"
                ) in {"scheduled", "processing"}:
                    column_updates["interview_transcript_status"] = str(
                        fireflies_state.get("status")
                    )
                await self._application_repository.update_admin_review(
                    application_id=application_id,
                    updates={"interview_schedule_options": updated_payload, **column_updates},
                )
                updated += 1

        if updated:
            logger.info("fireflies sync cycle completed updated=%s", updated)
        return updated

    async def _maybe_send_interview_thank_you_email(
        self,
        *,
        candidate: ApplicantApplication,
        fireflies_state: dict[str, Any],
        now_utc: datetime,
    ) -> bool:
        """Send post-interview thank-you email once per candidate transcript completion."""

        thank_you_state = (
            deepcopy(fireflies_state.get("thank_you_email"))
            if isinstance(fireflies_state.get("thank_you_email"), dict)
            else {}
        )
        status = str(thank_you_state.get("status") or "").strip().lower()
        if status == "sent":
            return False

        try:
            await self._email_sender.send_interview_participation_thanks(
                InterviewParticipationThanksEmail(
                    candidate_name=candidate.full_name,
                    candidate_email=str(candidate.email),
                    role_title=candidate.role_selection,
                )
            )
            fireflies_state["thank_you_email"] = {
                "status": "sent",
                "sent_at": now_utc.isoformat(),
                "last_error": None,
            }
            logger.info("post-interview thank-you email sent application_id=%s", candidate.id)
        except EmailSendError as exc:
            fireflies_state["thank_you_email"] = {
                "status": "failed",
                "last_attempt_at": now_utc.isoformat(),
                "last_error": str(exc)[:500],
            }
            logger.warning(
                "post-interview thank-you email failed application_id=%s error=%s",
                candidate.id,
                str(exc)[:200],
            )
        return True

    async def _fetch_fireflies_sync_application_ids(self) -> list[UUID]:
        """Return booked candidate IDs eligible for Fireflies transcript sync."""

        async with self._session_factory() as session:
            stmt = (
                select(ApplicantApplication.id)
                .where(
                    ApplicantApplication.interview_schedule_status == self._config.booked_status,
                    ApplicantApplication.interview_schedule_options.is_not(None),
                )
                .order_by(ApplicantApplication.updated_at.desc())
                .limit(max(1, self._config.fireflies.batch_size))
            )
            rows = await session.execute(stmt)
            return [row[0] for row in rows.all()]

    @staticmethod
    def _parse_datetime_safe(raw: Any) -> datetime | None:
        """Parse ISO datetime string into aware UTC datetime."""

        if not isinstance(raw, str) or not raw.strip():
            return None
        value = raw.strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _extract_confirmed_window(
        self, payload: dict[str, Any]
    ) -> tuple[datetime | None, datetime | None]:
        """Extract confirmed interview start/end from persisted options payload."""

        start_at = self._parse_datetime_safe(payload.get("confirmed_start_at"))
        end_at = self._parse_datetime_safe(payload.get("confirmed_end_at"))
        if start_at is not None and end_at is not None:
            return start_at, end_at

        selected = payload.get("selected_option_number")
        options = payload.get("options")
        if not isinstance(selected, int) or not isinstance(options, list):
            return None, None
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("option_number") != selected:
                continue
            return (
                self._parse_datetime_safe(option.get("start_at")),
                self._parse_datetime_safe(option.get("end_at")),
            )
        return None, None

    async def _fetch_expired_application_ids(self, *, now_utc: datetime) -> list[UUID]:
        """Return candidate ids whose interview holds are expired and unreconciled."""

        async with self._session_factory() as session:
            stmt = (
                select(ApplicantApplication.id)
                .where(
                    ApplicantApplication.interview_schedule_status.in_(
                        list(self._config.expiry_target_statuses)
                    ),
                    ApplicantApplication.interview_hold_expires_at.is_not(None),
                    ApplicantApplication.interview_hold_expires_at <= now_utc,
                )
                .order_by(ApplicantApplication.interview_hold_expires_at.asc())
                .limit(max(1, self._config.expiry_batch_size))
            )
            rows = await session.execute(stmt)
            return [row[0] for row in rows.all()]

    async def _fetch_reminder_application_ids(
        self,
        *,
        threshold_utc: datetime,
        now_utc: datetime,
    ) -> list[UUID]:
        """Return candidate ids eligible for one reminder before hold expiry."""

        async with self._session_factory() as session:
            stmt = (
                select(ApplicantApplication.id)
                .where(
                    ApplicantApplication.interview_schedule_status.in_(
                        list(self._config.reminder_target_statuses)
                    ),
                    ApplicantApplication.interview_schedule_sent_at.is_not(None),
                    ApplicantApplication.interview_schedule_sent_at <= threshold_utc,
                    ApplicantApplication.interview_hold_expires_at.is_not(None),
                    ApplicantApplication.interview_hold_expires_at > now_utc,
                )
                .order_by(ApplicantApplication.interview_schedule_sent_at.asc())
                .limit(max(1, self._config.reminder_batch_size))
            )
            rows = await session.execute(stmt)
            return [row[0] for row in rows.all()]


async def _run_worker() -> None:
    """Create dependencies and run expiry worker forever."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    scheduling = runtime_config.scheduling
    if not scheduling.enabled:
        raise RuntimeError("scheduling.enabled must be true to run interview hold expiry worker")

    session_factory = get_async_session_factory(runtime_config.postgres)
    application_repository = PostgresApplicationRepository(session_factory=session_factory)
    job_opening_repository = PostgresJobOpeningRepository(session_factory=session_factory)
    calendar_client = GoogleCalendarClient(
        service_account_json=settings.google_service_account_json,
        service_account_file=settings.google_service_account_file,
        oauth_client_id=settings.google_client_id,
        oauth_client_secret=settings.google_client_secret,
        oauth_refresh_token=settings.google_refresh_token,
        oauth_token_uri=runtime_config.google_api.token_uri,
    )
    fireflies_service = FirefliesService(
        api_key=settings.fireflies_api_key,
        config=scheduling.fireflies,
    )
    email_sender = get_email_sender()
    scheduling_service = InterviewSchedulingService(
        application_repository=application_repository,
        job_opening_repository=job_opening_repository,
        calendar_client=calendar_client,
        email_sender=email_sender,
        config=scheduling,
        security_config=runtime_config.security,
        confirmation_token_secret=(
            settings.interview_confirmation_token_secret or settings.admin_jwt_secret
        ),
        fireflies_service=fireflies_service,
    )
    worker = InterviewHoldExpiryWorker(
        config=scheduling,
        session_factory=session_factory,
        scheduling_service=scheduling_service,
        application_repository=application_repository,
        fireflies_service=fireflies_service,
        email_sender=email_sender,
    )
    await worker.run_forever()


def main() -> None:
    """Entrypoint for `python -m app.scripts.interview_hold_expiry_worker`."""

    asyncio.run(_run_worker())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
