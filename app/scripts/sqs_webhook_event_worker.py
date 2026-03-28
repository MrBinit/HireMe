"""SQS worker that processes deferred webhook/email side-effect jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from uuid import UUID

from app.api.deps import (
    get_application_service,
    get_email_sender,
    get_fireflies_service,
    get_webhook_event_dedupe_repository,
)
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.sqs_queue import SqsMessage, SqsQueueClient
from app.repositories.webhook_event_dedupe_repository import WebhookEventDedupeRepository
from app.services.application_service import ApplicationService
from app.services.email_sender import EmailSender
from app.services.fireflies_service import FirefliesService
from app.services.fireflies_webhook_service import FirefliesWebhookProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebhookWorkerTelemetry:
    """In-memory counters for webhook queue observability."""

    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    duplicates_completed: int = 0
    duplicates_in_progress: int = 0


class SqsWebhookEventWorker:
    """Long-poll SQS worker for deferred webhook + notification side effects."""

    def __init__(
        self,
        *,
        queue_client: SqsQueueClient,
        dedupe_repository: WebhookEventDedupeRepository,
        application_service: ApplicationService,
        email_sender: EmailSender,
        fireflies_service: FirefliesService,
        idempotency_processing_lock_seconds: int,
        max_in_flight: int,
        receive_batch_size: int,
        receive_wait_seconds: int,
        visibility_timeout_seconds: int,
    ) -> None:
        """Initialize worker dependencies and tuning values."""

        self._queue_client = queue_client
        self._dedupe_repository = dedupe_repository
        self._application_service = application_service
        self._fireflies_processor = FirefliesWebhookProcessor(
            service=application_service,
            email_sender=email_sender,
            fireflies_service=fireflies_service,
        )
        self._idempotency_processing_lock_seconds = max(1, idempotency_processing_lock_seconds)
        self._max_in_flight = max(1, max_in_flight)
        self._receive_batch_size = max(1, min(receive_batch_size, 10))
        self._receive_wait_seconds = max(0, min(receive_wait_seconds, 20))
        self._visibility_timeout_seconds = max(1, visibility_timeout_seconds)
        self._telemetry = WebhookWorkerTelemetry()
        self._telemetry_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        """Run worker forever and process queue messages in bounded parallelism."""

        logger.info(
            "starting webhook-event sqs worker with max_in_flight=%s batch_size=%s "
            "wait=%ss visibility=%ss",
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
                logger.exception("failed to receive webhook-event sqs messages; retrying")
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
        """Handle one deferred side-effect job with idempotent guardrails."""

        event_type, event_key, payload = self._extract_event(message)
        if event_type is None or event_key is None or payload is None:
            logger.warning("dropping invalid webhook-event message id=%s", message.message_id)
            await self._safe_delete(message)
            return

        claim = await self._dedupe_repository.claim_for_processing(
            event_key=event_key,
            source=event_type,
            stale_after_seconds=self._idempotency_processing_lock_seconds,
        )
        if claim == "already_completed":
            await self._track_duplicate(completed=True)
            await self._safe_delete(message)
            return
        if claim == "in_progress":
            await self._track_duplicate(completed=False)
            await self._safe_delete(message)
            return

        try:
            await self._dispatch_event(
                event_type=event_type,
                event_key=event_key,
                payload=payload,
            )
        except Exception as exc:
            await self._dedupe_repository.mark_failed(event_key=event_key, error=str(exc))
            await self._track_result(success=False)
            logger.exception(
                "webhook worker failed event_type=%s event_key=%s",
                event_type,
                event_key,
            )
            return

        await self._dedupe_repository.mark_completed(event_key=event_key)
        await self._track_result(success=True)
        await self._safe_delete(message)

    async def _dispatch_event(
        self,
        *,
        event_type: str,
        event_key: str,
        payload: dict[str, object],
    ) -> None:
        """Dispatch one typed webhook/event payload."""

        if event_type == "slack_team_join":
            slack_user_id = str(payload.get("slack_user_id") or "").strip()
            candidate_email = str(payload.get("candidate_email") or "").strip()
            if not slack_user_id or not candidate_email:
                raise ValueError("missing slack_team_join payload fields")
            await self._application_service.process_slack_team_join_event(
                slack_user_id=slack_user_id,
                candidate_email=candidate_email,
            )
            return

        if event_type == "fireflies_transcript_ready":
            meeting_id = str(payload.get("meeting_id") or "").strip()
            if not meeting_id:
                raise ValueError("missing fireflies_transcript_ready meeting_id")
            await self._fireflies_processor.process_meeting_id(meeting_id=meeting_id)
            return

        if event_type == "application_confirmation_email":
            raw_application_id = str(payload.get("application_id") or "").strip()
            if not raw_application_id:
                raise ValueError("missing application_confirmation_email application_id")
            application_id = UUID(raw_application_id)
            await self._application_service.send_application_confirmation_email_for_application(
                application_id=application_id
            )
            return

        logger.warning(
            "webhook worker ignoring unknown event_type=%s event_key=%s",
            event_type,
            event_key,
        )

    async def _safe_delete(self, message: SqsMessage) -> None:
        """Delete message and log failures without crashing worker loop."""

        try:
            await self._queue_client.delete_message(message.receipt_handle)
        except Exception:
            logger.exception("failed to delete webhook-event sqs message id=%s", message.message_id)

    async def _track_result(self, *, success: bool) -> None:
        """Update aggregate worker telemetry and emit periodic queue observability log."""

        async with self._telemetry_lock:
            self._telemetry.processed += 1
            if success:
                self._telemetry.succeeded += 1
            else:
                self._telemetry.failed += 1
            should_log = self._telemetry.processed % 25 == 0
            snapshot = WebhookWorkerTelemetry(
                processed=self._telemetry.processed,
                succeeded=self._telemetry.succeeded,
                failed=self._telemetry.failed,
                duplicates_completed=self._telemetry.duplicates_completed,
                duplicates_in_progress=self._telemetry.duplicates_in_progress,
            )

        if should_log:
            depth = await self._queue_client.get_approximate_queue_depth()
            logger.info(
                "webhook worker telemetry processed=%s success=%s failed=%s "
                "duplicate_completed=%s duplicate_in_progress=%s approx_queue_depth=%s",
                snapshot.processed,
                snapshot.succeeded,
                snapshot.failed,
                snapshot.duplicates_completed,
                snapshot.duplicates_in_progress,
                depth,
            )

    async def _track_duplicate(self, *, completed: bool) -> None:
        """Update duplicate counters for idempotency insights."""

        async with self._telemetry_lock:
            if completed:
                self._telemetry.duplicates_completed += 1
            else:
                self._telemetry.duplicates_in_progress += 1

    @staticmethod
    def _extract_event(
        message: SqsMessage,
    ) -> tuple[str | None, str | None, dict[str, object] | None]:
        """Extract event type/key/payload from queue message body."""

        try:
            body = json.loads(message.body)
        except json.JSONDecodeError:
            return None, None, None
        if not isinstance(body, dict):
            return None, None, None

        event_type = body.get("event_type")
        event_key = body.get("event_key")
        payload = body.get("payload")
        if not isinstance(event_type, str) or not event_type.strip():
            return None, None, None
        if not isinstance(event_key, str) or not event_key.strip():
            return None, None, None
        if not isinstance(payload, dict):
            return None, None, None
        return event_type.strip(), event_key.strip(), payload


async def _run_worker() -> None:
    """Create worker dependencies from runtime config and run forever."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    webhook_async = runtime_config.application.webhook_async

    if not webhook_async.enabled:
        raise RuntimeError("application.webhook_async.enabled must be true for webhook worker")
    if webhook_async.provider != "sqs":
        raise RuntimeError("application.webhook_async.provider must be 'sqs' for webhook worker")
    if not webhook_async.use_queue:
        raise RuntimeError("application.webhook_async.use_queue must be true for webhook worker")
    if not webhook_async.queue_url:
        raise RuntimeError("application.webhook_async.queue_url is required for webhook worker")

    queue_client = SqsQueueClient(
        queue_url=webhook_async.queue_url,
        region=webhook_async.region,
        endpoint_url=settings.sqs_endpoint_url,
    )
    worker = SqsWebhookEventWorker(
        queue_client=queue_client,
        dedupe_repository=get_webhook_event_dedupe_repository(),
        application_service=get_application_service(),
        email_sender=get_email_sender(),
        fireflies_service=get_fireflies_service(),
        idempotency_processing_lock_seconds=webhook_async.idempotency_processing_lock_seconds,
        max_in_flight=webhook_async.max_in_flight_per_worker,
        receive_batch_size=webhook_async.receive_batch_size,
        receive_wait_seconds=webhook_async.receive_wait_seconds,
        visibility_timeout_seconds=webhook_async.visibility_timeout_seconds,
    )
    await worker.run_forever()


def main() -> None:
    """Entrypoint for `python -m app.scripts.sqs_webhook_event_worker`."""

    asyncio.run(_run_worker())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
