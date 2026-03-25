"""PostgreSQL-backed repository for candidate applications."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.model.applicant_application import ApplicantApplication
from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
    extract_parse_projection,
)
from app.schemas.application import ApplicationRecord
from app.schemas.application import ApplicantStatus
from app.schemas.application import ParseStatus
from app.schemas.application import ResumeFileMeta


class PostgresApplicationRepository(ApplicationRepository):
    """Persist candidate applications in PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        """Initialize repository with session factory."""

        self._session_factory = session_factory

    async def create(self, record: ApplicationRecord) -> ApplicationRecord:
        """Insert application record while enforcing unique email+opening."""

        entity = ApplicantApplication(
            id=record.id,
            job_opening_id=record.job_opening_id,
            full_name=record.full_name,
            email=str(record.email),
            linkedin_url=str(record.linkedin_url) if record.linkedin_url else None,
            portfolio_url=str(record.portfolio_url),
            github_url=str(record.github_url),
            twitter_url=str(record.twitter_url) if record.twitter_url else None,
            role_selection=record.role_selection,
            resume_original_filename=record.resume.original_filename,
            resume_stored_filename=record.resume.stored_filename,
            resume_storage_path=record.resume.storage_path,
            resume_content_type=record.resume.content_type,
            resume_size_bytes=record.resume.size_bytes,
            parse_result=record.parse_result,
            parsed_skills=record.parsed_skills,
            parsed_education=record.parsed_education,
            latest_position=record.latest_position,
            total_years_experience=record.total_years_experience,
            parse_status=record.parse_status,
            applicant_status=record.applicant_status,
            reference_status=record.reference_status,
            created_at=record.created_at,
        )

        async with self._session_factory() as session:
            session.add(entity)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateApplicationError(
                    "application already exists for this email and job opening"
                ) from exc

        return record

    async def exists_for_email_and_opening(self, *, email: str, job_opening_id: UUID) -> bool:
        """Return True when email already applied to the specific opening."""

        normalized_email = email.strip().casefold()
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ApplicantApplication)
                .where(
                    ApplicantApplication.job_opening_id == job_opening_id,
                    func.lower(ApplicantApplication.email) == normalized_email,
                )
            )
            return int(result.scalar_one()) > 0

    async def list(
        self,
        *,
        offset: int,
        limit: int,
        job_opening_id: UUID | None = None,
    ) -> tuple[list[ApplicationRecord], int]:
        """Return paginated applications with optional job-opening filter."""

        async with self._session_factory() as session:
            count_stmt = select(func.count()).select_from(ApplicantApplication)
            if job_opening_id is not None:
                count_stmt = count_stmt.where(ApplicantApplication.job_opening_id == job_opening_id)
            total_result = await session.execute(count_stmt)
            total = int(total_result.scalar_one())

            select_stmt = (
                select(ApplicantApplication)
                .order_by(ApplicantApplication.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            if job_opening_id is not None:
                select_stmt = select_stmt.where(
                    ApplicantApplication.job_opening_id == job_opening_id
                )
            result = await session.execute(select_stmt)
            entities = list(result.scalars().all())
            return [self._to_record(entity) for entity in entities], total

    async def get_by_id(self, application_id: UUID) -> ApplicationRecord | None:
        """Return application by id, or None when not found."""

        async with self._session_factory() as session:
            entity = await session.get(ApplicantApplication, application_id)
            if entity is None:
                return None
            return self._to_record(entity)

    async def update_parse_state(
        self,
        *,
        application_id: UUID,
        parse_status: ParseStatus,
        parse_result: dict | None,
    ) -> bool:
        """Update parse status/result for one application."""

        async with self._session_factory() as session:
            entity = await session.get(ApplicantApplication, application_id)
            if entity is None:
                return False

            entity.parse_status = parse_status
            entity.parse_result = parse_result
            projection = extract_parse_projection(parse_result)
            entity.latest_position = projection["latest_position"]
            entity.total_years_experience = projection["total_years_experience"]
            entity.parsed_skills = projection["parsed_skills"]
            entity.parsed_education = projection["parsed_education"]
            await session.commit()
            return True

    async def update_reference_status(
        self,
        *,
        application_id: UUID,
        reference_status: bool,
    ) -> bool:
        """Update reference status for one application."""

        async with self._session_factory() as session:
            entity = await session.get(ApplicantApplication, application_id)
            if entity is None:
                return False

            entity.reference_status = reference_status
            await session.commit()
            return True

    async def update_applicant_status(
        self,
        *,
        application_id: UUID,
        applicant_status: ApplicantStatus,
    ) -> bool:
        """Update applicant lifecycle status for one application."""

        async with self._session_factory() as session:
            entity = await session.get(ApplicantApplication, application_id)
            if entity is None:
                return False

            entity.applicant_status = applicant_status
            await session.commit()
            return True

    @staticmethod
    def _to_record(entity: ApplicantApplication) -> ApplicationRecord:
        """Map ORM entity to response schema."""

        return ApplicationRecord(
            id=entity.id,
            job_opening_id=entity.job_opening_id,
            full_name=entity.full_name,
            email=entity.email,
            linkedin_url=entity.linkedin_url,
            portfolio_url=entity.portfolio_url,
            github_url=entity.github_url,
            twitter_url=entity.twitter_url,
            role_selection=entity.role_selection,
            parse_result=entity.parse_result,
            parse_status=entity.parse_status,
            applicant_status=entity.applicant_status,
            reference_status=entity.reference_status,
            latest_position=entity.latest_position,
            total_years_experience=entity.total_years_experience,
            parsed_skills=entity.parsed_skills,
            parsed_education=entity.parsed_education,
            resume=ResumeFileMeta(
                original_filename=entity.resume_original_filename,
                stored_filename=entity.resume_stored_filename,
                storage_path=entity.resume_storage_path,
                content_type=entity.resume_content_type,
                size_bytes=entity.resume_size_bytes,
            ),
            created_at=entity.created_at,
        )
