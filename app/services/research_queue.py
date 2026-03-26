"""Async queue abstractions for candidate research enrichment jobs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class ResearchQueuePublishError(RuntimeError):
    """Raised when research enrichment job publish to queue fails."""


@dataclass(frozen=True)
class CandidateResearchEnrichmentJob:
    """Queue payload for background candidate research enrichment."""

    application_id: UUID
    queued_at: datetime


class ResearchQueuePublisher(ABC):
    """Abstraction for research-enrichment queue publishers."""

    @abstractmethod
    async def publish(self, job: CandidateResearchEnrichmentJob) -> None:
        """Publish one candidate research enrichment job."""

        raise NotImplementedError


class NoopResearchQueuePublisher(ResearchQueuePublisher):
    """No-op publisher used when research enrichment queue is disabled."""

    async def publish(self, job: CandidateResearchEnrichmentJob) -> None:
        """Accept job without publishing."""

        _ = job
