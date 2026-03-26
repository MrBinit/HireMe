"""Async queue abstractions for candidate interview scheduling jobs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class SchedulingQueuePublishError(RuntimeError):
    """Raised when interview scheduling job publish to queue fails."""


@dataclass(frozen=True)
class CandidateInterviewSchedulingJob:
    """Queue payload for background interview scheduling orchestration."""

    application_id: UUID
    queued_at: datetime


class SchedulingQueuePublisher(ABC):
    """Abstraction for interview-scheduling queue publishers."""

    @abstractmethod
    async def publish(self, job: CandidateInterviewSchedulingJob) -> None:
        """Publish one interview scheduling job."""

        raise NotImplementedError


class NoopSchedulingQueuePublisher(SchedulingQueuePublisher):
    """No-op publisher used when interview scheduling queue is disabled."""

    async def publish(self, job: CandidateInterviewSchedulingJob) -> None:
        """Accept job without publishing."""

        _ = job
