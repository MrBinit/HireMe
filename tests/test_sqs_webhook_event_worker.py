"""Tests for deferred webhook-event SQS worker behavior."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from app.infra.sqs_queue import SqsMessage
from app.repositories.webhook_event_dedupe_repository import WebhookEventClaimResult
from app.scripts.sqs_webhook_event_worker import SqsWebhookEventWorker
from app.services.email_sender import NoopEmailSender


class _FakeQueueClient:
    """Queue client stub for worker unit tests."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def receive_messages(self, **kwargs):
        _ = kwargs
        return []

    async def delete_message(self, receipt_handle: str) -> None:
        self.deleted.append(receipt_handle)

    async def get_approximate_queue_depth(self) -> int | None:
        return 0


@dataclass
class _DedupeRecord:
    """Tracks idempotency actions in test fake repository."""

    claim_result: WebhookEventClaimResult = "acquired"
    completed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


class _FakeWebhookDedupeRepository:
    """Webhook idempotency repository fake with configurable claim result."""

    def __init__(self, *, claim_result: WebhookEventClaimResult = "acquired") -> None:
        self._record = _DedupeRecord(claim_result=claim_result)

    async def claim_for_processing(self, **kwargs) -> WebhookEventClaimResult:
        _ = kwargs
        return self._record.claim_result

    async def mark_completed(self, *, event_key: str) -> None:
        self._record.completed.append(event_key)

    async def mark_failed(self, *, event_key: str, error: str) -> None:
        self._record.failed.append((event_key, error))


class _FakeApplicationService:
    """Application service fake exposing worker-dispatched methods."""

    def __init__(self) -> None:
        self.slack_events: list[tuple[str, str]] = []
        self.confirmation_email_app_ids: list[str] = []

    async def process_slack_team_join_event(
        self, *, slack_user_id: str, candidate_email: str
    ) -> bool:
        self.slack_events.append((slack_user_id, candidate_email))
        return True

    async def send_application_confirmation_email_for_application(self, *, application_id) -> None:
        self.confirmation_email_app_ids.append(str(application_id))


class _FakeFirefliesService:
    """Minimal Fireflies service fake for worker construction."""

    enabled = True


def _message(payload: dict[str, object], *, receipt_handle: str = "rh-1") -> SqsMessage:
    """Build queue message from payload."""

    return SqsMessage(
        message_id="m1",
        receipt_handle=receipt_handle,
        body=json.dumps(payload),
    )


def test_webhook_worker_processes_slack_team_join_and_acks() -> None:
    """Worker should process valid slack event once and ack queue message."""

    async def run() -> None:
        queue_client = _FakeQueueClient()
        dedupe = _FakeWebhookDedupeRepository(claim_result="acquired")
        app_service = _FakeApplicationService()
        worker = SqsWebhookEventWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            dedupe_repository=dedupe,  # type: ignore[arg-type]
            application_service=app_service,  # type: ignore[arg-type]
            email_sender=NoopEmailSender(),
            fireflies_service=_FakeFirefliesService(),  # type: ignore[arg-type]
            idempotency_processing_lock_seconds=180,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=0,
            visibility_timeout_seconds=30,
        )
        message = _message(
            {
                "event_type": "slack_team_join",
                "event_key": "slack:team_join:Ev1",
                "payload": {
                    "slack_user_id": "U123",
                    "candidate_email": "candidate@example.com",
                },
            }
        )

        await worker._process_message(message)

        assert app_service.slack_events == [("U123", "candidate@example.com")]
        assert dedupe._record.completed == ["slack:team_join:Ev1"]
        assert dedupe._record.failed == []
        assert queue_client.deleted == ["rh-1"]

    asyncio.run(run())


def test_webhook_worker_skips_already_completed_duplicates() -> None:
    """Worker should ack duplicate events already marked completed."""

    async def run() -> None:
        queue_client = _FakeQueueClient()
        dedupe = _FakeWebhookDedupeRepository(claim_result="already_completed")
        app_service = _FakeApplicationService()
        worker = SqsWebhookEventWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            dedupe_repository=dedupe,  # type: ignore[arg-type]
            application_service=app_service,  # type: ignore[arg-type]
            email_sender=NoopEmailSender(),
            fireflies_service=_FakeFirefliesService(),  # type: ignore[arg-type]
            idempotency_processing_lock_seconds=180,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=0,
            visibility_timeout_seconds=30,
        )
        message = _message(
            {
                "event_type": "slack_team_join",
                "event_key": "slack:team_join:Ev2",
                "payload": {
                    "slack_user_id": "U456",
                    "candidate_email": "candidate2@example.com",
                },
            }
        )

        await worker._process_message(message)

        assert app_service.slack_events == []
        assert dedupe._record.completed == []
        assert dedupe._record.failed == []
        assert queue_client.deleted == ["rh-1"]

    asyncio.run(run())


def test_webhook_worker_marks_failure_and_keeps_message_for_retry() -> None:
    """Worker should mark failed processing and avoid ack so SQS can retry."""

    async def run() -> None:
        queue_client = _FakeQueueClient()
        dedupe = _FakeWebhookDedupeRepository(claim_result="acquired")
        app_service = _FakeApplicationService()
        worker = SqsWebhookEventWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            dedupe_repository=dedupe,  # type: ignore[arg-type]
            application_service=app_service,  # type: ignore[arg-type]
            email_sender=NoopEmailSender(),
            fireflies_service=_FakeFirefliesService(),  # type: ignore[arg-type]
            idempotency_processing_lock_seconds=180,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=0,
            visibility_timeout_seconds=30,
        )
        message = _message(
            {
                "event_type": "application_confirmation_email",
                "event_key": "application_confirmation_email:bad-id",
                "payload": {
                    "application_id": "not-a-uuid",
                },
            }
        )

        await worker._process_message(message)

        assert dedupe._record.completed == []
        assert len(dedupe._record.failed) == 1
        assert dedupe._record.failed[0][0] == "application_confirmation_email:bad-id"
        assert queue_client.deleted == []

    asyncio.run(run())
