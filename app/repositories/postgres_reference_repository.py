"""PostgreSQL-backed repository for applicant references."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.model.applicant_reference import ApplicantReference
from app.repositories.reference_repository import DuplicateReferenceError, ReferenceRepository
from app.schemas.reference import ReferenceRecord


class PostgresReferenceRepository(ReferenceRepository):
    """Persist applicant references in PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        """Initialize repository with async session factory."""

        self._session_factory = session_factory

    async def create(self, record: ReferenceRecord) -> ReferenceRecord:
        """Insert one reference record, preventing duplicate referee identity per application."""

        entity = ApplicantReference(
            id=record.id,
            application_id=record.application_id,
            candidate_email=str(record.candidate_email),
            candidate_name=record.candidate_name,
            candidate_position=record.candidate_position,
            referee_name=record.referee_name,
            referee_email=str(record.referee_email) if record.referee_email else None,
            referee_phone=record.referee_phone,
            referee_linkedin_url=(
                str(record.referee_linkedin_url) if record.referee_linkedin_url else None
            ),
            referee_company=record.referee_company,
            referee_position=record.referee_position,
            relationship=record.relationship,
            notes=record.notes,
            created_at=record.created_at,
        )

        async with self._session_factory() as session:
            session.add(entity)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateReferenceError(
                    "reference already exists for this application and referee identity"
                ) from exc

        return record

    async def list_by_application(
        self,
        *,
        application_id: UUID,
        offset: int,
        limit: int,
    ) -> tuple[list[ReferenceRecord], int]:
        """Return paginated references for one application."""

        async with self._session_factory() as session:
            total_result = await session.execute(
                select(func.count())
                .select_from(ApplicantReference)
                .where(ApplicantReference.application_id == application_id)
            )
            total = int(total_result.scalar_one())

            result = await session.execute(
                select(ApplicantReference)
                .where(ApplicantReference.application_id == application_id)
                .order_by(ApplicantReference.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            entities = list(result.scalars().all())
            return [self._to_record(entity) for entity in entities], total

    @staticmethod
    def _to_record(entity: ApplicantReference) -> ReferenceRecord:
        """Map ORM entity to response schema."""

        return ReferenceRecord(
            id=entity.id,
            application_id=entity.application_id,
            candidate_email=entity.candidate_email,
            candidate_name=entity.candidate_name,
            candidate_position=entity.candidate_position,
            referee_name=entity.referee_name,
            referee_email=entity.referee_email,
            referee_phone=entity.referee_phone,
            referee_linkedin_url=entity.referee_linkedin_url,
            referee_company=entity.referee_company,
            referee_position=entity.referee_position,
            relationship=entity.relationship,
            notes=entity.notes,
            created_at=entity.created_at,
        )
