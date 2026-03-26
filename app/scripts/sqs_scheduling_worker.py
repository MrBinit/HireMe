"""SQS worker that processes queued interview scheduling jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from app.api.deps import get_email_sender
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.database import get_async_session_factory
from app.infra.google_calendar_client import GoogleCalendarClient
from app.infra.sqs_queue import SqsMessage, SqsQueueClient
from app.repositories.postgres_application_repository import PostgresApplicationRepository
from app.repositories.postgres_job_opening_repository import PostgresJobOpeningRepository
from app.services.interview_scheduling_service import InterviewSchedulingService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class SqsInterviewSchedulingWorker:
    """Long-poll SQS worker for asynchronous interview option scheduling."""

    def __init__(
        self,
        *,
        queue_client: SqsQueueClient,
        service: InterviewSchedulingService,
        application_repository: PostgresApplicationRepository,
        max_in_flight: int,
        receive_batch_size: int,
        receive_wait_seconds: int,
        visibility_timeout_seconds: int,
    ):
        """Initialize worker dependencies and tuning values."""

        self._queue_client = queue_client
        self._service = service
        self._application_repository = application_repository
        self._max_in_flight = max(1, max_in_flight)
        self._receive_batch_size = max(1, min(receive_batch_size, 10))
        self._receive_wait_seconds = max(0, min(receive_wait_seconds, 20))
        self._visibility_timeout_seconds = max(1, visibility_timeout_seconds)

    async def run_forever(self) -> None:
        """Run worker forever and process messages in bounded parallelism."""

        logger.info(
            "starting scheduling sqs worker with "
            "max_in_flight=%s batch_size=%s wait=%ss visibility=%ss",
            self._max_in_flight,
            self._receive_batch_size,
            self._receive_wait_seconds,
            self._visibility_timeout_seconds,
        )
        semaphore = asyncio.Semaphore(self._max_in_flight)

        while True:
            try:
                messages = await self._queue_client.receive_messages(
                    max_number_of_messages=self._receive_batch_size,
                    wait_time_seconds=self._receive_wait_seconds,
                    visibility_timeout_seconds=self._visibility_timeout_seconds,
                )
            except Exception:
                logger.exception("failed to receive scheduling sqs messages; retrying")
                await asyncio.sleep(2)
                continue

            if not messages:
                continue

            tasks = [
                asyncio.create_task(self._process_with_semaphore(message, semaphore))
                for message in messages
            ]
            await asyncio.gather(*tasks)

    async def _process_with_semaphore(
        self,
        message: SqsMessage,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Process one message while honoring max in-flight bound."""

        async with semaphore:
            await self._process_message(message)

    async def _process_message(self, message: SqsMessage) -> None:
        """Run one interview scheduling orchestration job."""

        application_id = self._extract_application_id(message)
        if application_id is None:
            logger.warning("dropping invalid scheduling message id=%s", message.message_id)
            await self._safe_delete(message)
            return

        await self._application_repository.update_admin_review(
            application_id=application_id,
            updates={
                "interview_schedule_status": "in_progress",
                "interview_schedule_error": None,
            },
        )
        logger.info("scheduling worker application_id=%s job started", application_id)
        try:
            await self._service.create_options_for_candidate(application_id=application_id)
            logger.info("scheduling worker application_id=%s options sent", application_id)
        except Exception as exc:
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates={
                    "interview_schedule_status": "failed",
                    "interview_schedule_error": str(exc)[:1000],
                },
            )
            logger.exception("scheduling failed for application_id=%s", application_id)
            return

        await self._safe_delete(message)

    async def _safe_delete(self, message: SqsMessage) -> None:
        """Delete message and log failures without crashing worker loop."""

        try:
            await self._queue_client.delete_message(message.receipt_handle)
        except Exception:
            logger.exception("failed to delete scheduling sqs message id=%s", message.message_id)

    @staticmethod
    def _extract_application_id(message: SqsMessage) -> UUID | None:
        """Extract application UUID from queue message body."""

        try:
            payload = json.loads(message.body)
        except json.JSONDecodeError:
            return None

        raw_application_id = payload.get("application_id")
        if not isinstance(raw_application_id, str):
            return None

        try:
            return UUID(raw_application_id)
        except ValueError:
            return None


async def _run_worker() -> None:
    """Create worker dependencies from runtime config and run forever."""

    runtime_config = get_runtime_config()
    settings = get_settings()

    if not runtime_config.scheduling.enabled:
        raise RuntimeError("scheduling.enabled must be true to run scheduling sqs worker")
    if runtime_config.scheduling.provider != "sqs":
        raise RuntimeError("scheduling.provider must be 'sqs' to run scheduling sqs worker")
    if not runtime_config.scheduling.use_queue:
        raise RuntimeError("scheduling.use_queue must be true to run scheduling sqs worker")
    if not settings.sqs_scheduling_queue_url:
        raise RuntimeError("SQS_SCHEDULING_QUEUE_URL is required to run scheduling sqs worker")

    application_repository = PostgresApplicationRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )
    job_opening_repository = PostgresJobOpeningRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )
    calendar_client = GoogleCalendarClient(
        service_account_json=settings.google_service_account_json,
        service_account_file=settings.google_service_account_file,
        oauth_client_id=settings.google_client_id,
        oauth_client_secret=settings.google_client_secret,
        oauth_refresh_token=settings.google_refresh_token,
        oauth_token_uri=runtime_config.google_api.token_uri,
    )
    service = InterviewSchedulingService(
        application_repository=application_repository,
        job_opening_repository=job_opening_repository,
        calendar_client=calendar_client,
        email_sender=get_email_sender(),
        config=runtime_config.scheduling,
    )

    queue_client = SqsQueueClient(
        queue_url=settings.sqs_scheduling_queue_url,
        region=runtime_config.scheduling.region,
        endpoint_url=settings.sqs_endpoint_url,
    )
    worker = SqsInterviewSchedulingWorker(
        queue_client=queue_client,
        service=service,
        application_repository=application_repository,
        max_in_flight=runtime_config.scheduling.max_in_flight_per_worker,
        receive_batch_size=runtime_config.scheduling.receive_batch_size,
        receive_wait_seconds=runtime_config.scheduling.receive_wait_seconds,
        visibility_timeout_seconds=runtime_config.scheduling.visibility_timeout_seconds,
    )
    await worker.run_forever()


def main() -> None:
    """Entrypoint for `python -m app.scripts.sqs_scheduling_worker`."""

    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
