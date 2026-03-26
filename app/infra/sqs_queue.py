"""Async-friendly SQS queue publisher for parse jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
            endpoint_url=endpoint_url,
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
            endpoint_url=endpoint_url,
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
            endpoint_url=endpoint_url,
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
            endpoint_url=endpoint_url,
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
