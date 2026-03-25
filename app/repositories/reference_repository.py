"""Repository interface for applicant reference persistence."""

from abc import ABC, abstractmethod
from uuid import UUID

from app.schemas.reference import ReferenceRecord


class DuplicateReferenceError(ValueError):
    """Raised when a duplicate reference is submitted for one application."""


class ReferenceRepository(ABC):
    """Persistence operations for applicant references."""

    @abstractmethod
    async def create(self, record: ReferenceRecord) -> ReferenceRecord:
        """Persist and return one reference record."""

        raise NotImplementedError

    @abstractmethod
    async def list_by_application(
        self,
        *,
        application_id: UUID,
        offset: int,
        limit: int,
    ) -> tuple[list[ReferenceRecord], int]:
        """Return paginated references for one application id."""

        raise NotImplementedError
