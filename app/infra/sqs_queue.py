"""Async-friendly SQS queue publisher for parse jobs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any

import anyio
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.services.parse_queue import ParseQueuePublishError, ParseQueuePublisher, ResumeParseJob
from app.services.evaluation_queue import (
    CandidateEvaluationJob,
    EvaluationQueuePublishError,
    EvaluationQueuePublisher,
)
from app.services.research_queue import (
    CandidateResearchEnrichmentJob,
    ResearchQueuePublishError,
    ResearchQueuePublisher,
)
from app.services.scheduling_queue import (
    CandidateInterviewSchedulingJob,
    SchedulingQueuePublishError,
    SchedulingQueuePublisher,
)
from app.services.webhook_event_queue import (
    WebhookEventJob,
    WebhookEventQueueBackpressureError,
    WebhookEventQueuePublishError,
    WebhookEventQueuePublisher,
)

logger = logging.getLogger(__name__)


def _normalize_endpoint_url(value: str | None) -> str | None:
    """Convert blank endpoint strings to None for boto client compatibility."""

    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class SqsParseQueuePublisher(ParseQueuePublisher):
    """Publish resume parse jobs to Amazon SQS."""

    def __init__(
        self,
        *,
        queue_url: str,
        region: str,
        endpoint_url: str | None = None,
    ):
        """Initialize SQS client with queue URL and region."""

        self._queue_url = queue_url
        self._client = boto3.client(
            "sqs",
            region_name=region,
            endpoint_url=_normalize_endpoint_url(endpoint_url),
        )

    async def publish(self, job: ResumeParseJob) -> None:
        """Send a parse job message to SQS."""

        message_body = json.dumps(self._to_payload(job), default=str)

        def _run() -> None:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=message_body,
            )

        try:
            await anyio.to_thread.run_sync(_run)
        except (ClientError, BotoCoreError) as exc:
            raise ParseQueuePublishError("failed to publish resume parse job to SQS") from exc

    @staticmethod
    def _to_payload(job: ResumeParseJob) -> dict[str, Any]:
        """Serialize parse job into queue payload."""

        return {
            "event_type": "resume_parse_requested",
            "application_id": str(job.application_id),
            "job_opening_id": str(job.job_opening_id),
            "role_selection": job.role_selection,
            "email": job.email,
            "resume_storage_path": job.resume_storage_path,
            "created_at": job.created_at.isoformat(),
        }


class SqsEvaluationQueuePublisher(EvaluationQueuePublisher):
    """Publish candidate LLM evaluation jobs to Amazon SQS."""

    def __init__(
        self,
        *,
        queue_url: str,
        region: str,
        endpoint_url: str | None = None,
    ):
        """Initialize SQS client with queue URL and region."""

        self._queue_url = queue_url
        self._client = boto3.client(
            "sqs",
            region_name=region,
            endpoint_url=_normalize_endpoint_url(endpoint_url),
        )

    async def publish(self, job: CandidateEvaluationJob) -> None:
        """Send one candidate LLM evaluation job to SQS."""

        message_body = json.dumps(self._to_payload(job), default=str)

        def _run() -> None:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=message_body,
            )

        try:
            await anyio.to_thread.run_sync(_run)
        except (ClientError, BotoCoreError) as exc:
            raise EvaluationQueuePublishError(
                "failed to publish candidate evaluation job to SQS"
            ) from exc

    @staticmethod
    def _to_payload(job: CandidateEvaluationJob) -> dict[str, Any]:
        """Serialize candidate-evaluation job into queue payload."""

        return {
            "event_type": "candidate_evaluation_requested",
            "application_id": str(job.application_id),
            "queued_at": job.queued_at.isoformat(),
        }


class SqsResearchQueuePublisher(ResearchQueuePublisher):
    """Publish candidate research enrichment jobs to Amazon SQS."""

    def __init__(
        self,
        *,
        queue_url: str,
        region: str,
        endpoint_url: str | None = None,
    ):
        """Initialize SQS client with queue URL and region."""

        self._queue_url = queue_url
        self._client = boto3.client(
            "sqs",
            region_name=region,
            endpoint_url=_normalize_endpoint_url(endpoint_url),
        )

    async def publish(self, job: CandidateResearchEnrichmentJob) -> None:
        """Send one candidate research enrichment job to SQS."""

        message_body = json.dumps(self._to_payload(job), default=str)

        def _run() -> None:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=message_body,
            )

        try:
            await anyio.to_thread.run_sync(_run)
        except (ClientError, BotoCoreError) as exc:
            raise ResearchQueuePublishError(
                "failed to publish candidate research enrichment job to SQS"
            ) from exc

    @staticmethod
    def _to_payload(job: CandidateResearchEnrichmentJob) -> dict[str, Any]:
        """Serialize candidate-research job into queue payload."""

        return {
            "event_type": "candidate_research_enrichment_requested",
            "application_id": str(job.application_id),
            "queued_at": job.queued_at.isoformat(),
        }


class SqsSchedulingQueuePublisher(SchedulingQueuePublisher):
    """Publish interview scheduling jobs to Amazon SQS."""

    def __init__(
        self,
        *,
        queue_url: str,
        region: str,
        endpoint_url: str | None = None,
    ):
        """Initialize SQS client with queue URL and region."""

        self._queue_url = queue_url
        self._client = boto3.client(
            "sqs",
            region_name=region,
            endpoint_url=_normalize_endpoint_url(endpoint_url),
        )

    async def publish(self, job: CandidateInterviewSchedulingJob) -> None:
        """Send one candidate interview scheduling job to SQS."""

        message_body = json.dumps(self._to_payload(job), default=str)

        def _run() -> None:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=message_body,
            )

        try:
            await anyio.to_thread.run_sync(_run)
        except (ClientError, BotoCoreError) as exc:
            raise SchedulingQueuePublishError(
                "failed to publish candidate interview scheduling job to SQS"
            ) from exc

    @staticmethod
    def _to_payload(job: CandidateInterviewSchedulingJob) -> dict[str, Any]:
        """Serialize candidate-scheduling job into queue payload."""

        return {
            "event_type": "candidate_interview_scheduling_requested",
            "application_id": str(job.application_id),
            "queued_at": job.queued_at.isoformat(),
        }


class SqsWebhookEventQueuePublisher(WebhookEventQueuePublisher):
    """Publish deferred webhook/email side effects to Amazon SQS."""

    def __init__(
        self,
        *,
        queue_url: str,
        region: str,
        endpoint_url: str | None = None,
        queue_depth_warning_threshold: int = 500,
        queue_depth_reject_threshold: int = 2000,
        reject_when_queue_depth_exceeded: bool = False,
        queue_depth_cache_seconds: int = 5,
    ) -> None:
        """Initialize SQS client + lightweight queue-depth backpressure guard."""

        self._queue_url = queue_url
        self._client = boto3.client(
            "sqs",
            region_name=region,
            endpoint_url=_normalize_endpoint_url(endpoint_url),
        )
        self._queue_depth_warning_threshold = max(1, queue_depth_warning_threshold)
        self._queue_depth_reject_threshold = max(
            self._queue_depth_warning_threshold,
            queue_depth_reject_threshold,
        )
        self._reject_when_queue_depth_exceeded = bool(reject_when_queue_depth_exceeded)
        self._queue_depth_cache_seconds = max(0, queue_depth_cache_seconds)
        self._cached_depth: int | None = None
        self._cached_depth_at: float | None = None

    async def publish(self, job: WebhookEventJob) -> None:
        """Send one deferred side-effect job to SQS."""

        await self._enforce_backpressure()
        message_body = json.dumps(self._to_payload(job), default=str)

        def _run() -> None:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=message_body,
            )

        try:
            await anyio.to_thread.run_sync(_run)
        except WebhookEventQueuePublishError:
            raise
        except (ClientError, BotoCoreError) as exc:
            raise WebhookEventQueuePublishError(
                "failed to publish webhook-event job to SQS"
            ) from exc

    async def get_approximate_queue_depth(self) -> int | None:
        """Return approximate queue depth from SQS attributes."""

        depth, _ = await self._read_queue_depth(force_refresh=False)
        return depth

    async def _enforce_backpressure(self) -> None:
        """Warn or reject enqueue attempts based on approximate queue depth."""

        depth, refreshed = await self._read_queue_depth(force_refresh=False)
        if depth is None:
            return
        if depth >= self._queue_depth_warning_threshold:
            logger.warning(
                "webhook queue depth high depth=%s threshold=%s refreshed=%s",
                depth,
                self._queue_depth_warning_threshold,
                refreshed,
            )
        if self._reject_when_queue_depth_exceeded and depth >= self._queue_depth_reject_threshold:
            raise WebhookEventQueueBackpressureError("webhook queue backlog is high; retry shortly")

    async def _read_queue_depth(self, *, force_refresh: bool) -> tuple[int | None, bool]:
        """Read or reuse cached queue depth."""

        now = monotonic()
        if (
            not force_refresh
            and self._cached_depth is not None
            and self._cached_depth_at is not None
            and (now - self._cached_depth_at) <= self._queue_depth_cache_seconds
        ):
            return self._cached_depth, False

        def _run() -> int | None:
            response = self._client.get_queue_attributes(
                QueueUrl=self._queue_url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
            attrs = response.get("Attributes") or {}
            visible = int(str(attrs.get("ApproximateNumberOfMessages") or "0"))
            in_flight = int(str(attrs.get("ApproximateNumberOfMessagesNotVisible") or "0"))
            return visible + in_flight

        try:
            depth = await anyio.to_thread.run_sync(_run)
        except Exception:
            logger.exception("failed to read webhook queue attributes")
            return self._cached_depth, False

        self._cached_depth = depth
        self._cached_depth_at = now
        return depth, True

    @staticmethod
    def _to_payload(job: WebhookEventJob) -> dict[str, Any]:
        """Serialize webhook-event job into queue payload."""

        return {
            "event_type": job.event_type,
            "event_key": job.event_key,
            "payload": job.payload,
            "queued_at": (
                job.queued_at.isoformat()
                if isinstance(job.queued_at, datetime)
                else str(job.queued_at)
            ),
        }


@dataclass(frozen=True)
class SqsMessage:
    """Received SQS message payload."""

    message_id: str
    receipt_handle: str
    body: str


class SqsQueueClient:
    """Async-friendly SQS operations for worker consumers."""

    def __init__(
        self,
        *,
        queue_url: str,
        region: str,
        endpoint_url: str | None = None,
    ):
        """Initialize SQS client for receive/delete operations."""

        self._queue_url = queue_url
        self._client = boto3.client(
            "sqs",
            region_name=region,
            endpoint_url=_normalize_endpoint_url(endpoint_url),
        )

    async def receive_messages(
        self,
        *,
        max_number_of_messages: int,
        wait_time_seconds: int,
        visibility_timeout_seconds: int,
    ) -> list[SqsMessage]:
        """Receive up to N messages with long polling."""

        def _run() -> list[SqsMessage]:
            result = self._client.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max(1, min(max_number_of_messages, 10)),
                WaitTimeSeconds=max(0, min(wait_time_seconds, 20)),
                VisibilityTimeout=visibility_timeout_seconds,
                MessageAttributeNames=["All"],
            )
            messages: list[SqsMessage] = []
            for item in result.get("Messages", []):
                messages.append(
                    SqsMessage(
                        message_id=item.get("MessageId", ""),
                        receipt_handle=item.get("ReceiptHandle", ""),
                        body=item.get("Body", ""),
                    )
                )
            return messages

        try:
            return await anyio.to_thread.run_sync(_run)
        except (ClientError, BotoCoreError) as exc:
            raise ParseQueuePublishError("failed to receive messages from SQS") from exc

    async def delete_message(self, receipt_handle: str) -> None:
        """Delete one message from queue by receipt handle."""

        def _run() -> None:
            self._client.delete_message(
                QueueUrl=self._queue_url,
                ReceiptHandle=receipt_handle,
            )

        try:
            await anyio.to_thread.run_sync(_run)
        except (ClientError, BotoCoreError) as exc:
            raise ParseQueuePublishError("failed to delete message from SQS") from exc

    async def get_approximate_queue_depth(self) -> int | None:
        """Return approximate total queue depth (visible + in-flight)."""

        def _run() -> int:
            response = self._client.get_queue_attributes(
                QueueUrl=self._queue_url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
            attrs = response.get("Attributes") or {}
            visible = int(str(attrs.get("ApproximateNumberOfMessages") or "0"))
            in_flight = int(str(attrs.get("ApproximateNumberOfMessagesNotVisible") or "0"))
            return visible + in_flight

        try:
            return await anyio.to_thread.run_sync(_run)
        except Exception:
            logger.exception("failed to fetch sqs queue depth")
            return None
