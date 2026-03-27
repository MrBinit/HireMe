"""SQS worker that processes queued candidate research enrichment jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from app.core.runtime_config import BedrockRuntimeConfig
from app.core.runtime_config import ResearchRuntimeConfig
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.bedrock_runtime import BedrockRuntimeClient
from app.infra.sqs_queue import SqsMessage, SqsQueueClient
from app.scripts.enrich_shortlisted_llm_profiles import (
    _enrich_one_candidate,
    _load_candidates,
    _persist_payload,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class SqsResearchEnrichmentWorker:
    """Long-poll SQS worker for asynchronous candidate research enrichment."""

    def __init__(
        self,
        *,
        queue_client: SqsQueueClient,
        research_config: ResearchRuntimeConfig,
        bedrock_config: BedrockRuntimeConfig,
        bedrock_client: BedrockRuntimeClient | None,
        max_in_flight: int,
        receive_batch_size: int,
        receive_wait_seconds: int,
        visibility_timeout_seconds: int,
    ):
        """Initialize worker dependencies and tuning values."""

        self._queue_client = queue_client
        self._research_config = research_config
        self._bedrock_config = bedrock_config
        self._bedrock_client = bedrock_client
        self._max_in_flight = max(1, max_in_flight)
        self._receive_batch_size = max(1, min(receive_batch_size, 10))
        self._receive_wait_seconds = max(0, min(receive_wait_seconds, 20))
        self._visibility_timeout_seconds = max(1, visibility_timeout_seconds)

    async def run_forever(self) -> None:
        """Run the worker forever and process messages in bounded parallelism."""

        logger.info(
            "starting research enrichment sqs worker with "
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
                logger.exception("failed to receive research sqs messages; retrying")
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
        """Enrich one queued candidate and persist compact summary JSON."""

        application_id = self._extract_application_id(message)
        if application_id is None:
            logger.warning("dropping invalid research message id=%s", message.message_id)
            await self._safe_delete(message)
            return

        logger.info("research worker application_id=%s job started", application_id)
        try:
            candidates = await _load_candidates(
                config=self._research_config,
                offset=0,
                limit=1,
                application_ids=[application_id],
            )
            if not candidates:
                logger.warning(
                    "candidate not found while processing research application_id=%s",
                    application_id,
                )
                await self._safe_delete(message)
                return
            candidate = candidates[0]
            logger.info("research worker application_id=%s candidate loaded", application_id)
            payload = await _enrich_one_candidate(
                candidate=candidate,
                config=self._research_config,
                bedrock_client=self._bedrock_client,
                bedrock_config=self._bedrock_config,
            )
            logger.info("research worker application_id=%s enrich pipeline done", application_id)
            await _persist_payload(
                candidate_id=application_id,
                payload=payload,
                max_chars=self._research_config.enrichment.max_research_json_chars,
            )
            logger.info("research worker application_id=%s persisted", application_id)
        except Exception:
            logger.exception(
                "research enrichment failed for application_id=%s",
                application_id,
            )
            return

        await self._safe_delete(message)
        logger.info("research worker application_id=%s message acked", application_id)

    async def _safe_delete(self, message: SqsMessage) -> None:
        """Delete message and log failures without crashing worker loop."""

        try:
            await self._queue_client.delete_message(message.receipt_handle)
        except Exception:
            logger.exception("failed to delete research sqs message id=%s", message.message_id)

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
    enrichment_config = runtime_config.research.enrichment
    research_queue_url = enrichment_config.queue_url

    if not runtime_config.research.enabled:
        raise RuntimeError("research.enabled must be true to run research sqs worker")
    if enrichment_config.provider != "sqs":
        raise RuntimeError("research.enrichment.provider must be 'sqs' to run research sqs worker")
    if not enrichment_config.use_queue:
        raise RuntimeError("research.enrichment.use_queue must be true to run research sqs worker")
    if not research_queue_url:
        raise RuntimeError("research.enrichment.queue_url is required to run research sqs worker")
    if not settings.serpapi_api_key:
        raise RuntimeError("SERPAPI_API_KEY is required to run research sqs worker")

    bedrock_config = runtime_config.bedrock
    bedrock_client: BedrockRuntimeClient | None = None
    if runtime_config.research.enrichment.llm_analysis_enabled and bedrock_config.enabled:
        bedrock_client = BedrockRuntimeClient(
            region=bedrock_config.region,
            max_retries=bedrock_config.max_retries,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            aws_session_token=settings.aws_session_token,
            endpoint_url=settings.bedrock_endpoint_url,
        )

    queue_client = SqsQueueClient(
        queue_url=research_queue_url,
        region=enrichment_config.region,
        endpoint_url=settings.sqs_endpoint_url,
    )
    worker = SqsResearchEnrichmentWorker(
        queue_client=queue_client,
        research_config=runtime_config.research,
        bedrock_config=bedrock_config,
        bedrock_client=bedrock_client,
        max_in_flight=enrichment_config.max_in_flight_per_worker,
        receive_batch_size=enrichment_config.receive_batch_size,
        receive_wait_seconds=enrichment_config.receive_wait_seconds,
        visibility_timeout_seconds=enrichment_config.visibility_timeout_seconds,
    )
    await worker.run_forever()


def main() -> None:
    """Entrypoint for `python -m app.scripts.sqs_research_enrichment_worker`."""

    asyncio.run(_run_worker())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
