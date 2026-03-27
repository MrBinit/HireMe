"""Repository interface for application persistence."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from uuid import UUID

from app.schemas.application import ApplicationRecord
from app.schemas.application import ApplicantStatus
from app.schemas.application import ParseStatus


class DuplicateApplicationError(ValueError):
    """Raised when an application duplicates an existing email+job opening pair."""


class ApplicationRepository(ABC):
    """Persistence operations for candidate applications."""

    @abstractmethod
    async def create(self, record: ApplicationRecord) -> ApplicationRecord:
        """Persist and return an application record."""

        raise NotImplementedError

    @abstractmethod
    async def exists_for_email_and_opening(self, *, email: str, job_opening_id: UUID) -> bool:
        """Return True when an email has already applied to a given opening."""

        raise NotImplementedError

    @abstractmethod
    async def list(
        self,
        *,
        offset: int,
        limit: int,
        job_opening_id: UUID | None = None,
        role_selection: str | None = None,
        applicant_status: ApplicantStatus | None = None,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        keyword_search: str | None = None,
        min_total_years_experience: float | None = None,
        max_total_years_experience: float | None = None,
        experience_within_range: bool | None = None,
    ) -> tuple[list[ApplicationRecord], int]:
        """Return paginated applications and total count, optionally filtered by opening."""

        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, application_id: UUID) -> ApplicationRecord | None:
        """Return one application by id, or None when not found."""

        raise NotImplementedError

    @abstractmethod
    async def get_latest_by_email(self, *, email: str) -> ApplicationRecord | None:
        """Return most recent application by candidate email, or None."""

        raise NotImplementedError

    @abstractmethod
    async def update_parse_state(
        self,
        *,
        application_id: UUID,
        parse_status: ParseStatus,
        parse_result: dict | None,
        parsed_total_years_experience: float | None = None,
        parsed_search_text: str | None = None,
    ) -> bool:
        """Update parse fields and return True when the record exists."""

        raise NotImplementedError

    @abstractmethod
    async def update_reference_status(
        self,
        *,
        application_id: UUID,
        reference_status: bool,
    ) -> bool:
        """Update reference status and return True when the record exists."""

        raise NotImplementedError

    @abstractmethod
    async def update_applicant_status(
        self,
        *,
        application_id: UUID,
        applicant_status: ApplicantStatus,
        note: str | None = None,
    ) -> bool:
        """Update applicant lifecycle status and return True when record exists."""

        raise NotImplementedError

    @abstractmethod
    async def update_admin_review(
        self,
        *,
        application_id: UUID,
        updates: dict[str, Any],
    ) -> bool:
        """Update admin-review fields and return True when record exists."""

        raise NotImplementedError

    @abstractmethod
    async def transition_interview_schedule_status(
        self,
        *,
        application_id: UUID,
        from_statuses: set[str],
        to_status: str,
    ) -> bool:
        """Atomically transition interview_schedule_status when current value matches."""

        raise NotImplementedError
