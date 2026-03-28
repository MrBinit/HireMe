"""Repository abstractions for webhook idempotency state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal


WebhookEventClaimResult = Literal["acquired", "already_completed", "in_progress"]


class WebhookEventDedupeRepository(ABC):
    """Persistence operations for webhook event idempotency state."""

    @abstractmethod
    async def claim_for_processing(
        self,
        *,
        event_key: str,
        source: str,
        stale_after_seconds: int,
    ) -> WebhookEventClaimResult:
        """Attempt to claim one event key for processing."""

        raise NotImplementedError

    @abstractmethod
    async def mark_completed(self, *, event_key: str) -> None:
        """Mark one event key as fully processed."""

        raise NotImplementedError

    @abstractmethod
    async def mark_failed(self, *, event_key: str, error: str) -> None:
        """Mark one event key as failed for retry-aware visibility."""

        raise NotImplementedError
