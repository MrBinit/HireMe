"""SQS worker that processes queued candidate LLM evaluation jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import anyio
from app.core.error import ApplicationValidationError
from app.core.runtime_config import ApplicationRuntimeConfig, get_runtime_config
from app.core.settings import get_settings
from app.infra.bedrock_runtime import BedrockRuntimeClient
from app.infra.database import get_async_session_factory
from app.infra.sqs_queue import (
    SqsMessage,
    SqsQueueClient,
    SqsResearchQueuePublisher,
    SqsSchedulingQueuePublisher,
)
from app.repositories.postgres_application_repository import PostgresApplicationRepository
from app.repositories.postgres_job_opening_repository import PostgresJobOpeningRepository
from app.schemas.evaluation import CandidateEvaluationResult
from app.services.candidate_evaluation_service import CandidateEvaluationService
from app.services.research_queue import (
    CandidateResearchEnrichmentJob,
    NoopResearchQueuePublisher,
    ResearchQueuePublishError,
    ResearchQueuePublisher,
)
from app.services.scheduling_queue import (
    CandidateInterviewSchedulingJob,
    NoopSchedulingQueuePublisher,
    SchedulingQueuePublishError,
    SchedulingQueuePublisher,
)

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
        research_queue_publisher: ResearchQueuePublisher,
        research_queue_enabled: bool,
        research_target_statuses: set[str],
        research_enqueue_timeout_seconds: float,
        scheduling_queue_publisher: SchedulingQueuePublisher | None = None,
        scheduling_queue_enabled: bool = False,
        scheduling_target_statuses: set[str] | None = None,
        scheduling_enqueue_timeout_seconds: float = 2.0,
        ai_score_threshold: float,
        max_in_flight: int,
        receive_batch_size: int,
        receive_wait_seconds: int,
        visibility_timeout_seconds: int,
    ):
        """Initialize worker dependencies and tuning values."""

        self._queue_client = queue_client
        self._evaluator = evaluator
        self._application_repository = application_repository
        self._research_queue_publisher = research_queue_publisher
        self._research_queue_enabled = bool(research_queue_enabled)
        self._research_target_statuses = set(research_target_statuses)
        self._research_enqueue_timeout_seconds = max(0.1, research_enqueue_timeout_seconds)
        self._scheduling_queue_publisher = (
            scheduling_queue_publisher or NoopSchedulingQueuePublisher()
        )
        self._scheduling_queue_enabled = bool(scheduling_queue_enabled)
        self._scheduling_target_statuses = set(scheduling_target_statuses or [])
        self._scheduling_enqueue_timeout_seconds = max(0.1, scheduling_enqueue_timeout_seconds)
        self._ai_score_threshold = float(ai_score_threshold)
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

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            logger.warning(
                "candidate not found for queued evaluation message application_id=%s",
                application_id,
            )
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
            evaluation_score = float(evaluation.score)
            review_reasons = self._compute_manual_review_reasons(
                evaluation=evaluation,
                application_config=runtime_config.application,
            )
            needs_human_review = len(review_reasons) > 0
            summary = CandidateEvaluationService.format_evaluation_summary(evaluation)
            if needs_human_review:
                summary = f"{summary} [manual_review_reasons={','.join(review_reasons)}]"
            if len(summary) > 3900:
                summary = summary[:3900] + "..."
            updates: dict[str, object] = {
                "ai_score": evaluation_score,
                "ai_screening_summary": summary,
                "evaluation_status": "completed",
            }
            if needs_human_review:
                updates["applicant_status"] = "screened"
                updates["rejection_reason"] = None
            elif evaluation_score < float(runtime_config.application.ai_score_threshold):
                updates["applicant_status"] = "rejected"
                updates["rejection_reason"] = runtime_config.application.ai_score_fail_reason
            else:
                updates["applicant_status"] = "shortlisted"
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
            elif (not needs_human_review) and evaluation_score >= self._ai_score_threshold:
                await self._enqueue_research_if_eligible(application_id)
                await self._enqueue_scheduling_if_eligible(application_id)
            else:
                logger.info(
                    "evaluation worker application_id=%s "
                    "research enqueue skipped ai_score=%s threshold=%s review=%s reasons=%s",
                    application_id,
                    evaluation_score,
                    self._ai_score_threshold,
                    needs_human_review,
                    ",".join(review_reasons) if review_reasons else "",
                )
        except ApplicationValidationError as exc:
            error_message = str(exc)
            if error_message == "candidate application not found":
                logger.warning(
                    "candidate removed during evaluation; dropping message application_id=%s",
                    application_id,
                )
                await self._safe_delete(message)
                return

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

    @staticmethod
    def _compute_manual_review_reasons(
        *,
        evaluation: CandidateEvaluationResult,
        application_config: ApplicationRuntimeConfig,
    ) -> list[str]:
        """Return deterministic reasons requiring human review."""

        reasons: list[str] = []
        review_min = float(application_config.ai_score_manual_review_min)
        review_max = float(application_config.ai_score_manual_review_max)
        if review_min > review_max:
            review_min, review_max = review_max, review_min

        score = float(evaluation.score)
        if review_min <= score <= review_max:
            reasons.append("score_band")
        if float(evaluation.confidence) < float(
            application_config.ai_score_auto_decision_min_confidence
        ):
            reasons.append("low_confidence")
        if bool(evaluation.needs_human_review):
            reasons.append("model_flag")
        return reasons

    async def _enqueue_research_if_eligible(self, application_id: UUID) -> None:
        """Queue research enrichment only after successful AI evaluation threshold pass."""

        if not self._research_queue_enabled:
            return

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            logger.warning(
                "evaluation worker application_id=%s missing while checking research enqueue",
                application_id,
            )
            return
        if candidate.evaluation_status != "completed":
            logger.info(
                "evaluation worker application_id=%s research enqueue skipped evaluation_status=%s",
                application_id,
                candidate.evaluation_status,
            )
            return
        if candidate.ai_score is None or float(candidate.ai_score) < self._ai_score_threshold:
            logger.info(
                "evaluation worker application_id=%s "
                "research enqueue skipped ai_score=%s threshold=%s",
                application_id,
                candidate.ai_score,
                self._ai_score_threshold,
            )
            return
        if candidate.online_research_summary:
            logger.info(
                "evaluation worker application_id=%s "
                "research enqueue skipped existing_summary=true",
                application_id,
            )
            return
        if candidate.applicant_status not in self._research_target_statuses:
            logger.info(
                "evaluation worker application_id=%s research enqueue skipped status=%s allowed=%s",
                application_id,
                candidate.applicant_status,
                sorted(self._research_target_statuses),
            )
            return

        job = CandidateResearchEnrichmentJob(
            application_id=application_id,
            queued_at=datetime.now(tz=timezone.utc),
        )
        try:
            with anyio.fail_after(self._research_enqueue_timeout_seconds):
                await self._research_queue_publisher.publish(job)
            logger.info("evaluation worker application_id=%s research job queued", application_id)
        except (ResearchQueuePublishError, TimeoutError):
            logger.exception(
                "evaluation worker application_id=%s failed to queue research job",
                application_id,
            )
        except Exception:
            logger.exception(
                "evaluation worker application_id=%s unexpected research enqueue failure",
                application_id,
            )

    async def _enqueue_scheduling_if_eligible(self, application_id: UUID) -> None:
        """Queue interview scheduling after successful shortlist and threshold pass."""

        if not self._scheduling_queue_enabled:
            return

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            logger.warning(
                "evaluation worker application_id=%s missing while checking scheduling enqueue",
                application_id,
            )
            return
        if candidate.evaluation_status != "completed":
            logger.info(
                "evaluation worker application_id=%s "
                "scheduling enqueue skipped evaluation_status=%s",
                application_id,
                candidate.evaluation_status,
            )
            return
        if candidate.ai_score is None or float(candidate.ai_score) < self._ai_score_threshold:
            logger.info(
                "evaluation worker application_id=%s "
                "scheduling enqueue skipped ai_score=%s threshold=%s",
                application_id,
                candidate.ai_score,
                self._ai_score_threshold,
            )
            return
        if candidate.interview_schedule_status in {
            "queued",
            "in_progress",
            "interview_confirming",
            "interview_options_sent",
            "interview_email_sent",
            "options_sent",
            "interview_booked",
        }:
            logger.info(
                "evaluation worker application_id=%s "
                "scheduling enqueue skipped interview_schedule_status=%s",
                application_id,
                candidate.interview_schedule_status,
            )
            return
        if candidate.applicant_status not in self._scheduling_target_statuses:
            logger.info(
                "evaluation worker application_id=%s scheduling enqueue skipped status=%s allowed=%s",
                application_id,
                candidate.applicant_status,
                sorted(self._scheduling_target_statuses),
            )
            return

        job = CandidateInterviewSchedulingJob(
            application_id=application_id,
            queued_at=datetime.now(tz=timezone.utc),
        )
        try:
            with anyio.fail_after(self._scheduling_enqueue_timeout_seconds):
                await self._scheduling_queue_publisher.publish(job)
            await self._application_repository.update_admin_review(
                application_id=application_id,
                updates={
                    "interview_schedule_status": "queued",
                    "interview_schedule_error": None,
                },
            )
            logger.info(
                "evaluation worker application_id=%s scheduling job queued",
                application_id,
            )
        except (SchedulingQueuePublishError, TimeoutError):
            logger.exception(
                "evaluation worker application_id=%s failed to queue scheduling job",
                application_id,
            )
        except Exception:
            logger.exception(
                "evaluation worker application_id=%s unexpected scheduling enqueue failure",
                application_id,
            )

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
    evaluation_queue_url = runtime_config.evaluation.queue_url

    if runtime_config.evaluation.provider != "sqs":
        raise RuntimeError("evaluation.provider must be 'sqs' to run evaluation sqs worker")
    if not evaluation_queue_url:
        raise RuntimeError("evaluation.queue_url is required to run evaluation sqs worker")

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
        queue_url=evaluation_queue_url,
        region=runtime_config.evaluation.region,
        endpoint_url=settings.sqs_endpoint_url,
    )

    enrichment_config = runtime_config.research.enrichment
    research_queue_enabled = (
        runtime_config.research.enabled
        and enrichment_config.use_queue
        and enrichment_config.provider == "sqs"
        and bool(enrichment_config.queue_url)
    )
    research_queue_publisher: ResearchQueuePublisher = NoopResearchQueuePublisher()
    if research_queue_enabled and enrichment_config.queue_url:
        research_queue_publisher = SqsResearchQueuePublisher(
            queue_url=enrichment_config.queue_url,
            region=enrichment_config.region,
            endpoint_url=settings.sqs_endpoint_url,
        )
    elif runtime_config.research.enabled and enrichment_config.use_queue:
        logger.warning(
            "research queue requested but unavailable; "
            "evaluation worker cannot auto-queue enrichment"
        )

    scheduling_config = runtime_config.scheduling
    scheduling_queue_enabled = (
        scheduling_config.enabled
        and scheduling_config.auto_enqueue_after_shortlist
        and scheduling_config.use_queue
        and scheduling_config.provider == "sqs"
        and bool(scheduling_config.queue_url)
    )
    scheduling_queue_publisher: SchedulingQueuePublisher = NoopSchedulingQueuePublisher()
    if scheduling_queue_enabled and scheduling_config.queue_url:
        scheduling_queue_publisher = SqsSchedulingQueuePublisher(
            queue_url=scheduling_config.queue_url,
            region=scheduling_config.region,
            endpoint_url=settings.sqs_endpoint_url,
        )
    elif scheduling_config.enabled and scheduling_config.use_queue:
        logger.warning(
            "scheduling queue requested but unavailable; "
            "evaluation worker cannot auto-queue interview scheduling"
        )

    worker = SqsEvaluationWorker(
        queue_client=queue_client,
        evaluator=evaluator,
        application_repository=application_repository,
        research_queue_publisher=research_queue_publisher,
        research_queue_enabled=research_queue_enabled,
        research_target_statuses=set(enrichment_config.target_statuses),
        research_enqueue_timeout_seconds=enrichment_config.enqueue_timeout_seconds,
        scheduling_queue_publisher=scheduling_queue_publisher,
        scheduling_queue_enabled=scheduling_queue_enabled,
        scheduling_target_statuses=set(scheduling_config.target_statuses),
        scheduling_enqueue_timeout_seconds=scheduling_config.enqueue_timeout_seconds,
        ai_score_threshold=runtime_config.application.ai_score_threshold,
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
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
