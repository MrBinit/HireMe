"""Async queue abstractions for candidate LLM evaluation jobs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class EvaluationQueuePublishError(RuntimeError):
    """Raised when LLM evaluation job publish to queue fails."""


@dataclass(frozen=True)
class CandidateEvaluationJob:
    """Queue payload for background candidate LLM evaluation."""

    application_id: UUID
    queued_at: datetime


class EvaluationQueuePublisher(ABC):
    """Abstraction for candidate-evaluation queue publishers."""

    @abstractmethod
    async def publish(self, job: CandidateEvaluationJob) -> None:
        """Publish one candidate LLM evaluation job."""

        raise NotImplementedError


class NoopEvaluationQueuePublisher(EvaluationQueuePublisher):
    """No-op publisher used when evaluation queue is disabled."""

    async def publish(self, job: CandidateEvaluationJob) -> None:
        """Accept job without publishing."""

        _ = job
