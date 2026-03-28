"""Queue abstractions for deferred webhook and notification side effects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


WebhookEventType = Literal[
    "slack_team_join",
    "fireflies_transcript_ready",
    "application_confirmation_email",
]


class WebhookEventQueuePublishError(RuntimeError):
    """Raised when a webhook-event job publish fails."""


class WebhookEventQueueBackpressureError(WebhookEventQueuePublishError):
    """Raised when queue depth exceeds configured backpressure threshold."""


@dataclass(frozen=True)
class WebhookEventJob:
    """Payload for one deferred webhook or notification side-effect job."""

    event_type: WebhookEventType
    event_key: str
    payload: dict[str, Any]
    queued_at: datetime


class WebhookEventQueuePublisher(ABC):
    """Abstraction for publishing deferred webhook/notification jobs."""

    @abstractmethod
    async def publish(self, job: WebhookEventJob) -> None:
        """Publish one webhook-event job."""

        raise NotImplementedError

    async def get_approximate_queue_depth(self) -> int | None:
        """Return approximate queue depth when supported by provider."""

        return None


class NoopWebhookEventQueuePublisher(WebhookEventQueuePublisher):
    """No-op publisher used when webhook job queueing is disabled."""

    async def publish(self, job: WebhookEventJob) -> None:
        """Accept job without publishing."""

        _ = job
