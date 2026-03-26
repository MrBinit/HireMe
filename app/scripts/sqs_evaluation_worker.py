"""SQS worker that processes queued candidate LLM evaluation jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.bedrock_runtime import BedrockRuntimeClient
from app.infra.database import get_async_session_factory
from app.infra.sqs_queue import SqsMessage, SqsQueueClient
from app.repositories.postgres_application_repository import PostgresApplicationRepository
from app.repositories.postgres_job_opening_repository import PostgresJobOpeningRepository
from app.services.candidate_evaluation_service import CandidateEvaluationService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class SqsEvaluationWorker:
    """Long-poll SQS worker for asynchronous candidate LLM evaluation."""

    def __init__(
        self,
        *,
        queue_client: SqsQueueClient,
        evaluator: CandidateEvaluationService,
        application_repository: PostgresApplicationRepository,
        max_in_flight: int,
        receive_batch_size: int,
        receive_wait_seconds: int,
        visibility_timeout_seconds: int,
    ):
        """Initialize worker dependencies and tuning values."""

        self._queue_client = queue_client
        self._evaluator = evaluator
        self._application_repository = application_repository
        self._max_in_flight = max(1, max_in_flight)
        self._receive_batch_size = max(1, min(receive_batch_size, 10))
        self._receive_wait_seconds = max(0, min(receive_wait_seconds, 20))
        self._visibility_timeout_seconds = max(1, visibility_timeout_seconds)

    async def run_forever(self) -> None:
        """Run the worker forever and process messages in bounded parallelism."""

        logger.info(
            "starting evaluation sqs worker with "
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
                logger.exception("failed to receive evaluation sqs messages; retrying")
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
        """Evaluate one queued candidate and persist score fields."""

        application_id = self._extract_application_id(message)
        if application_id is None:
            logger.warning("dropping invalid evaluation message id=%s", message.message_id)
            await self._safe_delete(message)
            return

        runtime_config = get_runtime_config()
        await self._application_repository.update_admin_review(
            application_id=application_id,
            updates={
                "evaluation_status": "in_progress",
            },
        )
        try:
            evaluation = await self._evaluator.evaluate_application(application_id=application_id)
            summary = CandidateEvaluationService.format_evaluation_summary(evaluation)
            updates: dict[str, object] = {
                "ai_score": float(evaluation.score),
                "ai_screening_summary": summary,
                "evaluation_status": "completed",
            }
            if float(evaluation.score) < float(runtime_config.application.ai_score_threshold):
                updates["applicant_status"] = "rejected"
                updates["rejection_reason"] = runtime_config.application.ai_score_fail_reason
            else:
                updates["rejection_reason"] = None

            updated = await self._application_repository.update_admin_review(
                application_id=application_id,
                updates=updates,
            )
            if not updated:
                logger.warning(
                    "candidate not found while persisting evaluation application_id=%s",
                    application_id,
                )
        except Exception:
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates={
                    "evaluation_status": "failed",
                },
            )
            logger.exception(
                "evaluation failed for application_id=%s",
                application_id,
            )
            return

        await self._safe_delete(message)

    async def _safe_delete(self, message: SqsMessage) -> None:
        """Delete message and log failures without crashing worker loop."""

        try:
            await self._queue_client.delete_message(message.receipt_handle)
        except Exception:
            logger.exception("failed to delete evaluation sqs message id=%s", message.message_id)

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

    if runtime_config.evaluation.provider != "sqs":
        raise RuntimeError("evaluation.provider must be 'sqs' to run evaluation sqs worker")
    if not settings.sqs_evaluation_queue_url:
        raise RuntimeError("SQS_EVALUATION_QUEUE_URL is required to run evaluation sqs worker")

    application_repository = PostgresApplicationRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )
    job_opening_repository = PostgresJobOpeningRepository(
        session_factory=get_async_session_factory(runtime_config.postgres)
    )
    evaluator = CandidateEvaluationService(
        application_repository=application_repository,
        job_opening_repository=job_opening_repository,
        bedrock_client=BedrockRuntimeClient(
            region=runtime_config.bedrock.region,
            max_retries=runtime_config.bedrock.max_retries,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            aws_session_token=settings.aws_session_token,
            endpoint_url=settings.bedrock_endpoint_url,
        ),
        bedrock_config=runtime_config.bedrock,
        evaluation_config=runtime_config.evaluation,
        application_config=runtime_config.application,
    )

    queue_client = SqsQueueClient(
        queue_url=settings.sqs_evaluation_queue_url,
        region=runtime_config.evaluation.region,
        endpoint_url=settings.sqs_endpoint_url,
    )
    worker = SqsEvaluationWorker(
        queue_client=queue_client,
        evaluator=evaluator,
        application_repository=application_repository,
        max_in_flight=runtime_config.evaluation.max_in_flight_per_worker,
        receive_batch_size=runtime_config.evaluation.receive_batch_size,
        receive_wait_seconds=runtime_config.evaluation.receive_wait_seconds,
        visibility_timeout_seconds=runtime_config.evaluation.visibility_timeout_seconds,
    )
    await worker.run_forever()


def main() -> None:
    """Entrypoint for `python -m app.scripts.sqs_evaluation_worker`."""

    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
