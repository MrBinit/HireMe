"""Business logic for job opening workflows."""

from __future__ import annotations

import re
from datetime import timezone
from datetime import datetime

from app.core.error import JobOpeningValidationError
from app.core.runtime_config import JobOpeningRuntimeConfig
from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.job_opening import (
    JobOpeningCreatePayload,
    JobOpeningListResponse,
    JobOpeningRecord,
)


class JobOpeningService:
    """Service layer for creating and listing job openings."""

    def __init__(self, repository: JobOpeningRepository, config: JobOpeningRuntimeConfig):
        """Initialize service with repository and runtime config."""

        self._repository = repository
        self._config = config

    async def create(self, payload: JobOpeningCreatePayload) -> JobOpeningRecord:
        """Create a job opening after validation."""

        normalized_payload = self._normalize_payload(payload)
        self._validate_payload(normalized_payload)

        if await self._repository.exists_role_title(normalized_payload.role_title):
            raise JobOpeningValidationError(
                f"role_title '{normalized_payload.role_title}' already exists"
            )

        created = await self._repository.create(normalized_payload)
        return self._with_status(created)

    async def list(self, *, offset: int = 0, limit: int | None = None) -> JobOpeningListResponse:
        """Return paginated job openings."""

        effective_limit = limit or self._config.default_list_limit
        if offset < 0:
            raise JobOpeningValidationError("offset must be >= 0")
        if effective_limit <= 0:
            raise JobOpeningValidationError("limit must be >= 1")
        if effective_limit > self._config.max_list_limit:
            raise JobOpeningValidationError(
                f"limit cannot be greater than {self._config.max_list_limit}"
            )

        items, total = await self._repository.list(offset=offset, limit=effective_limit)
        return JobOpeningListResponse(
            items=[self._with_status(item) for item in items],
            total=total,
            offset=offset,
            limit=effective_limit,
        )

    async def delete(self, job_opening_id: str) -> bool:
        """Delete opening by UUID string and return deletion status."""

        from uuid import UUID

        try:
            parsed_id = UUID(job_opening_id)
        except ValueError as exc:
            raise JobOpeningValidationError("job_opening_id must be a valid UUID") from exc
        return await self._repository.delete(parsed_id)

    async def set_paused(self, job_opening_id: str, paused: bool) -> JobOpeningRecord | None:
        """Pause or resume one job opening by UUID string."""

        from uuid import UUID

        try:
            parsed_id = UUID(job_opening_id)
        except ValueError as exc:
            raise JobOpeningValidationError("job_opening_id must be a valid UUID") from exc

        updated = await self._repository.set_paused(parsed_id, paused)
        if updated is None:
            return None
        return self._with_status(updated)

    async def list_role_titles(self) -> list[str]:
        """Return available role titles for application selection."""

        titles = await self._repository.list_role_titles()
        return sorted(set(titles))

    async def get_opening_for_role(self, role_title: str) -> JobOpeningRecord | None:
        """Return opening that matches a role title."""

        opening = await self._repository.find_by_role_title(role_title)
        if opening is None:
            return None
        return self._with_status(opening)

    def _normalize_payload(self, payload: JobOpeningCreatePayload) -> JobOpeningCreatePayload:
        """Trim and normalize user input before validation."""

        responsibilities = [item.strip() for item in payload.responsibilities if item.strip()]
        requirements = [item.strip() for item in payload.requirements if item.strip()]
        return JobOpeningCreatePayload(
            role_title=payload.role_title.strip(),
            manager_email=payload.manager_email.strip().lower(),
            team=payload.team.strip(),
            location=payload.location.strip(),
            experience_level=payload.experience_level.strip().lower(),
            experience_range=payload.experience_range.strip().lower(),
            application_open_at=self._to_utc(payload.application_open_at),
            application_close_at=self._to_utc(payload.application_close_at),
            responsibilities=responsibilities,
            requirements=requirements,
        )

    def _validate_payload(self, payload: JobOpeningCreatePayload) -> None:
        """Enforce runtime-config constraints for job openings."""

        if payload.experience_level not in self._config.allowed_experience_levels:
            raise JobOpeningValidationError(
                "experience_level must be one of "
                f"{', '.join(self._config.allowed_experience_levels)}"
            )

        if not re.match(self._config.experience_range_pattern, payload.experience_range):
            raise JobOpeningValidationError(
                "experience_range must look like '2-3 years' or '4-8 years'"
            )
        if payload.application_close_at <= payload.application_open_at:
            raise JobOpeningValidationError(
                "application_close_at must be later than application_open_at"
            )

        years_parts = re.findall(r"\d+", payload.experience_range)
        if len(years_parts) == 2:
            start_year, end_year = int(years_parts[0]), int(years_parts[1])
            if start_year > end_year:
                raise JobOpeningValidationError(
                    "experience_range start year cannot be greater than end year"
                )

        if len(payload.responsibilities) < self._config.min_bullet_items:
            raise JobOpeningValidationError(
                f"responsibilities must contain at least {self._config.min_bullet_items} items"
            )
        if len(payload.requirements) < self._config.min_bullet_items:
            raise JobOpeningValidationError(
                f"requirements must contain at least {self._config.min_bullet_items} items"
            )

        if len(payload.responsibilities) > self._config.max_bullet_items:
            raise JobOpeningValidationError(
                "responsibilities exceed max allowed items " f"({self._config.max_bullet_items})"
            )
        if len(payload.requirements) > self._config.max_bullet_items:
            raise JobOpeningValidationError(
                f"requirements exceed max allowed items ({self._config.max_bullet_items})"
            )

    @staticmethod
    def _to_utc(value):
        """Normalize datetime to timezone-aware UTC."""

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _resolve_status(record: JobOpeningRecord) -> str:
        """Compute whether the opening is currently open or closed."""

        if record.paused:
            return "paused"
        now = datetime.now(tz=timezone.utc)
        is_open = record.application_open_at <= now <= record.application_close_at
        return "open" if is_open else "closed"

    def _with_status(self, record: JobOpeningRecord) -> JobOpeningRecord:
        """Return record with runtime open/closed status."""

        return record.model_copy(update={"status": self._resolve_status(record)})
