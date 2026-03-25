"""Tests for reference submission service behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.error import ReferenceValidationError
from app.repositories.reference_repository import ReferenceRepository
from app.schemas.application import ApplicationRecord, ResumeFileMeta
from app.schemas.reference import ReferenceCreatePayload, ReferenceRecord
from app.services.reference_service import ReferenceService


class _FakeApplicationLookup:
    """Minimal application lookup used by reference service tests."""

    def __init__(self, record: ApplicationRecord | None):
        self._record = record
        self.updated_reference_status: bool | None = None

    async def get_by_id(self, application_id: UUID) -> ApplicationRecord | None:
        if self._record is None:
            return None
        if self._record.id != application_id:
            return None
        return self._record

    async def update_reference_status(
        self,
        *,
        application_id: UUID,
        reference_status: bool,
    ) -> bool:
        if self._record is None:
            return False
        if self._record.id != application_id:
            return False
        self.updated_reference_status = reference_status
        return True


class _InMemoryReferenceRepository(ReferenceRepository):
    """In-memory reference repository for service tests."""

    def __init__(self) -> None:
        self._records: list[ReferenceRecord] = []

    async def create(self, record: ReferenceRecord) -> ReferenceRecord:
        self._records.append(record)
        return record

    async def list_by_application(
        self,
        *,
        application_id: UUID,
        offset: int,
        limit: int,
    ) -> tuple[list[ReferenceRecord], int]:
        items = [item for item in self._records if item.application_id == application_id]
        total = len(items)
        return items[offset : offset + limit], total


def _application_record() -> ApplicationRecord:
    """Create baseline application record for tests."""

    app_id = uuid4()
    return ApplicationRecord(
        id=app_id,
        job_opening_id=uuid4(),
        full_name="Candidate Name",
        email="candidate@example.com",
        linkedin_url="https://www.linkedin.com/in/candidate",
        portfolio_url="https://candidate.dev",
        github_url="https://github.com/candidate",
        twitter_url=None,
        role_selection="Backend Engineer",
        parse_result=None,
        parse_status="pending",
        applicant_status="received",
        reference_status=False,
        resume=ResumeFileMeta(
            original_filename="resume.pdf",
            stored_filename=f"{app_id}.pdf",
            storage_path=f"s3://hireme-cv-bucket/hireme/resumes/{app_id}.pdf",
            content_type="application/pdf",
            size_bytes=100,
        ),
        created_at=datetime.now(tz=timezone.utc),
    )


def test_reference_create_succeeds_when_candidate_email_matches_application() -> None:
    """Reference creation should succeed when candidate email matches application."""

    async def run() -> None:
        application = _application_record()
        app_lookup = _FakeApplicationLookup(application)
        service = ReferenceService(
            repository=_InMemoryReferenceRepository(),
            application_repository=app_lookup,
        )

        created = await service.create(
            ReferenceCreatePayload(
                application_id=application.id,
                candidate_email="candidate@example.com",
                referee_name="Referee One",
                referee_email="ref1@example.com",
                relationship="manager",
            )
        )
        assert created.application_id == application.id
        assert str(created.candidate_email) == "candidate@example.com"
        assert created.referee_name == "Referee One"
        assert app_lookup.updated_reference_status is True

    asyncio.run(run())


def test_reference_create_rejects_when_candidate_email_mismatch() -> None:
    """Reference creation should reject mismatched candidate email."""

    async def run() -> None:
        application = _application_record()
        service = ReferenceService(
            repository=_InMemoryReferenceRepository(),
            application_repository=_FakeApplicationLookup(application),
        )

        error = None
        try:
            await service.create(
                ReferenceCreatePayload(
                    application_id=application.id,
                    candidate_email="wrong@example.com",
                    referee_name="Referee One",
                    referee_email="ref1@example.com",
                )
            )
        except ReferenceValidationError as exc:
            error = exc

        assert error is not None
        assert "candidate_email must match" in str(error)

    asyncio.run(run())
