"""PostgreSQL-backed repository for job openings."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.model.job_opening import JobOpening
from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.job_opening import JobOpeningCreatePayload, JobOpeningRecord


class PostgresJobOpeningRepository(JobOpeningRepository):
    """Persist and query job openings in PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        """Initialize repository with an async SQLAlchemy session factory."""

        self._session_factory = session_factory

    async def create(self, payload: JobOpeningCreatePayload) -> JobOpeningRecord:
        """Create and return a persisted job opening."""

        now = datetime.now(tz=timezone.utc)
        entity = JobOpening(
            id=uuid4(),
            role_title=payload.role_title,
            team=payload.team,
            location=payload.location,
            experience_level=payload.experience_level,
            experience_range=payload.experience_range,
            application_open_at=payload.application_open_at,
            application_close_at=payload.application_close_at,
            paused=False,
            responsibilities=payload.responsibilities,
            requirements=payload.requirements,
            created_at=now,
            updated_at=now,
        )

        async with self._session_factory() as session:
            session.add(entity)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(f"role_title '{payload.role_title}' already exists") from exc
            return self._to_record(entity)

    async def get(self, job_opening_id: UUID) -> JobOpeningRecord | None:
        """Fetch opening by UUID."""

        async with self._session_factory() as session:
            entity = await session.get(JobOpening, job_opening_id)
            if entity is None:
                return None
            return self._to_record(entity)

    async def delete(self, job_opening_id: UUID) -> bool:
        """Delete opening by UUID and return whether it existed."""

        async with self._session_factory() as session:
            result = await session.execute(
                delete(JobOpening).where(JobOpening.id == job_opening_id)
            )
            deleted = (result.rowcount or 0) > 0
            if deleted:
                await session.commit()
            else:
                await session.rollback()
            return deleted

    async def set_paused(self, job_opening_id: UUID, paused: bool) -> JobOpeningRecord | None:
        """Set paused state for one opening."""

        async with self._session_factory() as session:
            entity = await session.get(JobOpening, job_opening_id)
            if entity is None:
                return None
            entity.paused = paused
            await session.commit()
            await session.refresh(entity)
            return self._to_record(entity)

    async def list(self, *, offset: int, limit: int) -> tuple[list[JobOpeningRecord], int]:
        """Return paginated openings and total count."""

        async with self._session_factory() as session:
            total_result = await session.execute(select(func.count()).select_from(JobOpening))
            total = int(total_result.scalar_one())

            result = await session.execute(
                select(JobOpening)
                .order_by(JobOpening.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            entities = list(result.scalars().all())
            return [self._to_record(entity) for entity in entities], total

    async def find_by_role_title(self, role_title: str) -> JobOpeningRecord | None:
        """Fetch opening by case-insensitive role title."""

        normalized = role_title.strip().casefold()
        async with self._session_factory() as session:
            result = await session.execute(
                select(JobOpening).where(func.lower(JobOpening.role_title) == normalized).limit(1)
            )
            entity = result.scalar_one_or_none()
            if entity is None:
                return None
            return self._to_record(entity)

    async def list_role_titles(self) -> list[str]:
        """Return all persisted role titles."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(JobOpening.role_title).order_by(JobOpening.created_at)
            )
            return [str(title).strip() for title in result.scalars().all() if str(title).strip()]

    async def exists_role_title(self, role_title: str) -> bool:
        """Return True when role title already exists."""

        return await self.find_by_role_title(role_title) is not None

    @staticmethod
    def _to_record(entity: JobOpening) -> JobOpeningRecord:
        """Map ORM entity to API/schema record."""

        return JobOpeningRecord(
            id=entity.id,
            role_title=entity.role_title,
            team=entity.team,
            location=entity.location,
            experience_level=entity.experience_level,
            experience_range=entity.experience_range,
            application_open_at=entity.application_open_at,
            application_close_at=entity.application_close_at,
            paused=entity.paused,
            responsibilities=list(entity.responsibilities),
            requirements=list(entity.requirements),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
