"""Business logic for candidate reference submission."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from uuid import uuid4

from app.core.error import ReferenceValidationError
from app.repositories.application_repository import ApplicationRepository
from app.repositories.reference_repository import DuplicateReferenceError, ReferenceRepository
from app.schemas.reference import ReferenceCreatePayload, ReferenceListResponse, ReferenceRecord


class ReferenceService:
    """Service layer for creating and listing applicant references."""

    def __init__(
        self,
        *,
        repository: ReferenceRepository,
        application_repository: ApplicationRepository,
    ):
        """Initialize service with repositories."""

        self._repository = repository
        self._application_repository = application_repository

    async def create(self, payload: ReferenceCreatePayload) -> ReferenceRecord:
        """Create reference after validating candidate identity and application binding."""

        application = await self._application_repository.get_by_id(payload.application_id)
        if application is None:
            raise ReferenceValidationError("application_id was not found")

        candidate_email = str(payload.candidate_email).strip().casefold()
        application_email = str(application.email).strip().casefold()
        if candidate_email != application_email:
            raise ReferenceValidationError(
                "candidate_email must match the candidate application email"
            )

        record = ReferenceRecord(
            id=uuid4(),
            application_id=payload.application_id,
            candidate_email=payload.candidate_email,
            referee_name=payload.referee_name.strip(),
            referee_email=payload.referee_email,
            referee_phone=payload.referee_phone.strip() if payload.referee_phone else None,
            referee_linkedin_url=payload.referee_linkedin_url,
            referee_company=payload.referee_company.strip() if payload.referee_company else None,
            referee_position=payload.referee_position.strip() if payload.referee_position else None,
            relationship=payload.relationship.strip() if payload.relationship else None,
            notes=payload.notes.strip() if payload.notes else None,
            created_at=datetime.now(tz=timezone.utc),
        )

        try:
            created = await self._repository.create(record)
        except DuplicateReferenceError as exc:
            raise ReferenceValidationError(
                "duplicate reference: this referee already exists for the candidate application"
            ) from exc

        updated = await self._application_repository.update_reference_status(
            application_id=payload.application_id,
            reference_status=True,
        )
        if not updated:
            raise ReferenceValidationError("failed to update candidate reference status")

        return created

    async def list(
        self,
        *,
        application_id: UUID,
        offset: int = 0,
        limit: int = 20,
    ) -> ReferenceListResponse:
        """Return paginated references for one candidate application."""

        if offset < 0:
            raise ReferenceValidationError("offset must be >= 0")
        if limit <= 0:
            raise ReferenceValidationError("limit must be >= 1")
        if limit > 100:
            raise ReferenceValidationError("limit cannot be greater than 100")

        items, total = await self._repository.list_by_application(
            application_id=application_id,
            offset=offset,
            limit=limit,
        )
        return ReferenceListResponse(
            items=items,
            total=total,
            offset=offset,
            limit=limit,
        )
