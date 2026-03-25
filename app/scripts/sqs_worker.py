"""SQS worker that processes queued resume parse jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.database import get_async_session_factory
from app.infra.s3_store import S3ObjectStore
from app.infra.sqs_queue import SqsMessage, SqsQueueClient
from app.repositories.postgres_application_repository import PostgresApplicationRepository
from app.services.parse_processor import ResumeParseProcessor
from app.services.resume_extractor import LangChainResumeExtractor
from app.services.resume_structured_extractor import ResumeStructuredExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class SqsParseWorker:
    """Long-poll SQS worker for asynchronous resume parsing."""

    def __init__(
        self,
        *,
        queue_client: SqsQueueClient,
        parse_processor: ResumeParseProcessor,
        max_in_flight: int,
        receive_batch_size: int,
        receive_wait_seconds: int,
        visibility_timeout_seconds: int,
    ):
        """Initialize worker dependencies and tuning values."""

        self._queue_client = queue_client
        self._parse_processor = parse_processor
        self._max_in_flight = max(1, max_in_flight)
        self._receive_batch_size = max(1, min(receive_batch_size, 10))
        self._receive_wait_seconds = max(0, min(receive_wait_seconds, 20))
        self._visibility_timeout_seconds = max(1, visibility_timeout_seconds)

    async def run_forever(self) -> None:
        """Run the worker forever and process messages in bounded parallelism."""

        logger.info(
            "starting sqs worker with max_in_flight=%s batch_size=%s wait=%ss visibility=%ss",
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
                logger.exception("failed to receive sqs messages; retrying")
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
        """Parse one queue message and update parse lifecycle."""

        application_id = self._extract_application_id(message)
        if application_id is None:
            logger.warning("dropping invalid message id=%s", message.message_id)
            await self._safe_delete(message)
            return

        try:
            exists = await self._parse_processor.process(application_id)
        except Exception:
            logger.exception("parse failed for application_id=%s", application_id)
            return

        if not exists:
            logger.warning(
                "application not found for queued message application_id=%s", application_id
            )

        await self._safe_delete(message)

    async def _safe_delete(self, message: SqsMessage) -> None:
        """Delete message and log failures without crashing worker loop."""

        try:
            await self._queue_client.delete_message(message.receipt_handle)
        except Exception:
            logger.exception("failed to delete sqs message id=%s", message.message_id)

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

    if runtime_config.parse.provider != "sqs":
        raise RuntimeError("parse.provider must be 'sqs' to run sqs worker")
    if not settings.sqs_parse_queue_url:
        raise RuntimeError("SQS_PARSE_QUEUE_URL is required to run sqs worker")

    repository = PostgresApplicationRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )
    extractor = LangChainResumeExtractor(
        s3_store=S3ObjectStore(config=runtime_config.s3),
        max_extracted_chars=runtime_config.parse.max_extracted_chars,
    )
    structured_extractor = ResumeStructuredExtractor(
        section_aliases=runtime_config.parse.section_aliases,
        link_rules=runtime_config.parse.link_rules,
        max_section_lines=runtime_config.parse.max_section_lines,
    )
    processor = ResumeParseProcessor(
        repository=repository,
        extractor=extractor,
        structured_extractor=structured_extractor,
        llm_fallback_min_chars=runtime_config.parse.llm_fallback_min_chars,
    )
    queue_client = SqsQueueClient(
        queue_url=settings.sqs_parse_queue_url,
        region=runtime_config.parse.region,
        endpoint_url=settings.sqs_endpoint_url,
    )
    worker = SqsParseWorker(
        queue_client=queue_client,
        parse_processor=processor,
        max_in_flight=runtime_config.parse.max_in_flight_per_worker,
        receive_batch_size=runtime_config.parse.receive_batch_size,
        receive_wait_seconds=runtime_config.parse.receive_wait_seconds,
        visibility_timeout_seconds=runtime_config.parse.visibility_timeout_seconds,
    )
    await worker.run_forever()


def main() -> None:
    """Entrypoint for `python -m app.scripts.sqs_worker`."""

    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
