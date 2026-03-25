"""Repository interface for application persistence."""

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from app.schemas.application import ApplicationRecord
from app.schemas.application import ApplicantStatus
from app.schemas.application import ParseStatus


class DuplicateApplicationError(ValueError):
    """Raised when an application duplicates an existing email+job opening pair."""


def extract_parse_projection(parse_result: dict | None) -> dict[str, Any]:
    """Extract denormalized parse-summary fields from parse_result JSON."""

    if not isinstance(parse_result, dict):
        return {
            "latest_position": None,
            "total_years_experience": None,
            "parsed_skills": None,
            "parsed_education": None,
        }

    structured = parse_result.get("structured")
    if not isinstance(structured, dict):
        return {
            "latest_position": None,
            "total_years_experience": None,
            "parsed_skills": None,
            "parsed_education": None,
        }

    position = structured.get("position")
    years = structured.get("total_years_experience")
    skills = structured.get("skills")
    education = structured.get("education")

    latest_position = position.strip() if isinstance(position, str) and position.strip() else None
    total_years_experience = float(years) if isinstance(years, (int, float)) else None
    parsed_skills = [str(item) for item in skills] if isinstance(skills, list) else None
    parsed_education = education if isinstance(education, list) else None

    return {
        "latest_position": latest_position,
        "total_years_experience": total_years_experience,
        "parsed_skills": parsed_skills,
        "parsed_education": parsed_education,
    }


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
    ) -> tuple[list[ApplicationRecord], int]:
        """Return paginated applications and total count, optionally filtered by opening."""

        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, application_id: UUID) -> ApplicationRecord | None:
        """Return one application by id, or None when not found."""

        raise NotImplementedError

    @abstractmethod
    async def update_parse_state(
        self,
        *,
        application_id: UUID,
        parse_status: ParseStatus,
        parse_result: dict | None,
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
    ) -> bool:
        """Update applicant lifecycle status and return True when record exists."""

        raise NotImplementedError
