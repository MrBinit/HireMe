"""Repository interface for job opening persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from app.schemas.job_opening import JobOpeningCreatePayload, JobOpeningRecord


class JobOpeningRepository(ABC):
    """Persistence operations for job openings."""

    @abstractmethod
    async def create(self, payload: JobOpeningCreatePayload) -> JobOpeningRecord:
        """Persist and return a job opening."""

        raise NotImplementedError

    @abstractmethod
    async def get(self, job_opening_id: UUID) -> JobOpeningRecord | None:
        """Fetch opening by id, if it exists."""

        raise NotImplementedError

    @abstractmethod
    async def delete(self, job_opening_id: UUID) -> bool:
        """Delete opening by id and return whether it existed."""

        raise NotImplementedError

    @abstractmethod
    async def list(self, *, offset: int, limit: int) -> tuple[list[JobOpeningRecord], int]:
        """Return paginated job openings and total count."""

        raise NotImplementedError

    @abstractmethod
    async def find_by_role_title(self, role_title: str) -> JobOpeningRecord | None:
        """Fetch opening by role title, if it exists."""

        raise NotImplementedError

    @abstractmethod
    async def list_role_titles(self) -> list[str]:
        """Return all role titles currently available."""

        raise NotImplementedError

    @abstractmethod
    async def exists_role_title(self, role_title: str) -> bool:
        """Return True when role title already exists."""

        raise NotImplementedError
