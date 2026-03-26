"""Background worker to release expired interview hold events."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
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
from app.services.interview_scheduling_service import InterviewSchedulingService

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
    ) -> None:
        """Initialize expiry worker dependencies."""

        self._config = config
        self._session_factory = session_factory
        self._scheduling_service = scheduling_service

    async def run_forever(self) -> None:
        """Run expiry reconciliation loop forever."""

        logger.info(
            "starting interview hold expiry worker poll=%ss batch=%s statuses=%s",
            self._config.expiry_poll_interval_seconds,
            self._config.expiry_batch_size,
            self._config.expiry_target_statuses,
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

        reminder_count = await self._send_pending_reminders()
        if not self._config.auto_release_expired_holds:
            return reminder_count

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
        return reminder_count + released

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
    scheduling_service = InterviewSchedulingService(
        application_repository=application_repository,
        job_opening_repository=job_opening_repository,
        calendar_client=calendar_client,
        email_sender=get_email_sender(),
        config=scheduling,
        security_config=runtime_config.security,
        confirmation_token_secret=(
            settings.interview_confirmation_token_secret or settings.admin_jwt_secret
        ),
    )
    worker = InterviewHoldExpiryWorker(
        config=scheduling,
        session_factory=session_factory,
        scheduling_service=scheduling_service,
    )
    await worker.run_forever()


def main() -> None:
    """Entrypoint for `python -m app.scripts.interview_hold_expiry_worker`."""

    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
