"""Async parse-queue publishing abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class ParseQueuePublishError(RuntimeError):
    """Raised when parse job publish to queue fails."""


@dataclass(frozen=True)
class ResumeParseJob:
    """Queue payload for background resume parsing."""

    application_id: UUID
    job_opening_id: UUID
    role_selection: str
    email: str
    resume_storage_path: str
    created_at: datetime


class ParseQueuePublisher(ABC):
    """Abstraction for parse-job queue publishers."""

    @abstractmethod
    async def publish(self, job: ResumeParseJob) -> None:
        """Publish a resume parse job."""

        raise NotImplementedError


class NoopParseQueuePublisher(ParseQueuePublisher):
    """No-op publisher used when queueing is disabled."""

    async def publish(self, job: ResumeParseJob) -> None:
        """Accept job without publishing."""

        _ = job
