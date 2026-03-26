"""PostgreSQL-backed repository for candidate applications."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.model.applicant_application import ApplicantApplication
from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
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
            portfolio_url=str(record.portfolio_url) if record.portfolio_url else None,
            github_url=str(record.github_url),
            twitter_url=str(record.twitter_url) if record.twitter_url else None,
            role_selection=record.role_selection,
            resume_original_filename=record.resume.original_filename,
            resume_stored_filename=record.resume.stored_filename,
            resume_storage_path=record.resume.storage_path,
            resume_content_type=record.resume.content_type,
            resume_size_bytes=record.resume.size_bytes,
            parse_result=record.parse_result,
            parsed_total_years_experience=record.parsed_total_years_experience,
            parsed_search_text=record.parsed_search_text,
            rejection_reason=record.rejection_reason,
            ai_score=record.ai_score,
            ai_screening_summary=record.ai_screening_summary,
            candidate_brief=record.candidate_brief,
            online_research_summary=record.online_research_summary,
            interview_schedule_status=record.interview_schedule_status,
            interview_schedule_options=record.interview_schedule_options,
            interview_schedule_sent_at=record.interview_schedule_sent_at,
            interview_hold_expires_at=record.interview_hold_expires_at,
            interview_calendar_email=record.interview_calendar_email,
            interview_schedule_error=record.interview_schedule_error,
            status_history=[
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in record.status_history
            ],
            parse_status=record.parse_status,
            evaluation_status=record.evaluation_status,
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
        role_selection: str | None = None,
        applicant_status: ApplicantStatus | None = None,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        keyword_search: str | None = None,
        min_total_years_experience: float | None = None,
        max_total_years_experience: float | None = None,
        experience_within_range: bool | None = None,
    ) -> tuple[list[ApplicationRecord], int]:
        """Return paginated applications with optional job-opening filter."""

        filters = self._build_filters(
            job_opening_id=job_opening_id,
            role_selection=role_selection,
            applicant_status=applicant_status,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            keyword_search=keyword_search,
            min_total_years_experience=min_total_years_experience,
            max_total_years_experience=max_total_years_experience,
            experience_within_range=experience_within_range,
        )

        async with self._session_factory() as session:
            count_stmt = select(func.count()).select_from(ApplicantApplication).where(*filters)
            total_result = await session.execute(count_stmt)
            total = int(total_result.scalar_one())

            select_stmt = (
                select(ApplicantApplication)
                .order_by(ApplicantApplication.created_at.desc())
                .offset(offset)
                .limit(limit)
                .where(*filters)
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
        parsed_total_years_experience: float | None = None,
        parsed_search_text: str | None = None,
    ) -> bool:
        """Update parse status/result for one application."""

        async with self._session_factory() as session:
            entity = await session.get(ApplicantApplication, application_id)
            if entity is None:
                return False

            entity.parse_status = parse_status
            entity.parse_result = parse_result
            entity.parsed_total_years_experience = parsed_total_years_experience
            entity.parsed_search_text = parsed_search_text
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
        note: str | None = None,
    ) -> bool:
        """Update applicant lifecycle status for one application."""

        return await self.update_admin_review(
            application_id=application_id,
            updates={
                "applicant_status": applicant_status,
                "note": note,
            },
        )

    async def update_admin_review(
        self,
        *,
        application_id: UUID,
        updates: dict[str, Any],
    ) -> bool:
        """Update admin-review fields for one application."""

        async with self._session_factory() as session:
            entity = await session.get(ApplicantApplication, application_id)
            if entity is None:
                return False

            if "applicant_status" in updates and updates["applicant_status"] is not None:
                entity.applicant_status = updates["applicant_status"]
            if "rejection_reason" in updates:
                entity.rejection_reason = updates["rejection_reason"]
            if "evaluation_status" in updates:
                entity.evaluation_status = updates["evaluation_status"]
            if "ai_score" in updates:
                entity.ai_score = updates["ai_score"]
            if "ai_screening_summary" in updates:
                entity.ai_screening_summary = updates["ai_screening_summary"]
            if "candidate_brief" in updates:
                entity.candidate_brief = updates["candidate_brief"]
            if "online_research_summary" in updates:
                entity.online_research_summary = updates["online_research_summary"]
            if "interview_schedule_status" in updates:
                entity.interview_schedule_status = updates["interview_schedule_status"]
            if "interview_schedule_options" in updates:
                entity.interview_schedule_options = updates["interview_schedule_options"]
            if "interview_schedule_sent_at" in updates:
                entity.interview_schedule_sent_at = updates["interview_schedule_sent_at"]
            if "interview_hold_expires_at" in updates:
                entity.interview_hold_expires_at = updates["interview_hold_expires_at"]
            if "interview_calendar_email" in updates:
                entity.interview_calendar_email = updates["interview_calendar_email"]
            if "interview_schedule_error" in updates:
                entity.interview_schedule_error = updates["interview_schedule_error"]

            note = updates.get("note")
            if ("applicant_status" in updates and updates["applicant_status"] is not None) or note:
                history = list(entity.status_history or [])
                history.append(
                    {
                        "status": entity.applicant_status,
                        "note": note,
                        "changed_at": datetime.now(tz=timezone.utc)
                        .isoformat()
                        .replace(
                            "+00:00",
                            "Z",
                        ),
                        "source": "admin",
                    }
                )
                entity.status_history = history

            await session.commit()
            return True

    async def transition_interview_schedule_status(
        self,
        *,
        application_id: UUID,
        from_statuses: set[str],
        to_status: str,
    ) -> bool:
        """Atomically transition interview schedule status when expected state matches."""

        normalized_from = {value for value in from_statuses if isinstance(value, str) and value}
        if not normalized_from:
            return False

        async with self._session_factory() as session:
            stmt = (
                update(ApplicantApplication)
                .where(
                    ApplicantApplication.id == application_id,
                    ApplicantApplication.interview_schedule_status.in_(list(normalized_from)),
                )
                .values(
                    interview_schedule_status=to_status,
                    interview_schedule_error=None,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0) > 0

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
            parsed_total_years_experience=entity.parsed_total_years_experience,
            parsed_search_text=entity.parsed_search_text,
            parse_status=entity.parse_status,
            evaluation_status=entity.evaluation_status,
            applicant_status=entity.applicant_status,
            rejection_reason=entity.rejection_reason,
            ai_score=entity.ai_score,
            ai_screening_summary=entity.ai_screening_summary,
            candidate_brief=entity.candidate_brief,
            online_research_summary=entity.online_research_summary,
            interview_schedule_status=entity.interview_schedule_status,
            interview_schedule_options=entity.interview_schedule_options,
            interview_schedule_sent_at=entity.interview_schedule_sent_at,
            interview_hold_expires_at=entity.interview_hold_expires_at,
            interview_calendar_email=entity.interview_calendar_email,
            interview_schedule_error=entity.interview_schedule_error,
            status_history=entity.status_history or [],
            reference_status=entity.reference_status,
            resume=ResumeFileMeta(
                original_filename=entity.resume_original_filename,
                stored_filename=entity.resume_stored_filename,
                storage_path=entity.resume_storage_path,
                content_type=entity.resume_content_type,
                size_bytes=entity.resume_size_bytes,
            ),
            created_at=entity.created_at,
        )

    @staticmethod
    def _normalize_keyword_terms(keyword_search: str | None) -> list[str]:
        """Normalize keyword search string into de-duplicated lowercase terms."""

        if not keyword_search:
            return []
        seen: set[str] = set()
        terms: list[str] = []
        for raw in keyword_search.split():
            term = raw.strip().casefold()
            if len(term) < 2 or term in seen:
                continue
            seen.add(term)
            terms.append(term)
        return terms

    def _build_filters(
        self,
        *,
        job_opening_id: UUID | None,
        role_selection: str | None,
        applicant_status: ApplicantStatus | None,
        submitted_from: datetime | None,
        submitted_to: datetime | None,
        keyword_search: str | None,
        min_total_years_experience: float | None,
        max_total_years_experience: float | None,
        experience_within_range: bool | None,
    ) -> list:
        """Build SQLAlchemy where clauses for application listing filters."""

        filters: list = []
        if job_opening_id is not None:
            filters.append(ApplicantApplication.job_opening_id == job_opening_id)
        if role_selection is not None:
            filters.append(
                func.lower(ApplicantApplication.role_selection) == role_selection.strip().casefold()
            )
        if applicant_status is not None:
            filters.append(ApplicantApplication.applicant_status == applicant_status)
        if submitted_from is not None:
            filters.append(ApplicantApplication.created_at >= submitted_from)
        if submitted_to is not None:
            filters.append(ApplicantApplication.created_at <= submitted_to)

        terms = self._normalize_keyword_terms(keyword_search)
        if terms:
            normalized_search = func.lower(
                func.coalesce(ApplicantApplication.parsed_search_text, "")
            )
            filters.append(or_(*[normalized_search.like(f"%{term}%") for term in terms]))

        experience_column = ApplicantApplication.parsed_total_years_experience
        if min_total_years_experience is not None or max_total_years_experience is not None:
            within_parts = [experience_column.is_not(None)]
            if min_total_years_experience is not None:
                within_parts.append(experience_column >= min_total_years_experience)
            if max_total_years_experience is not None:
                within_parts.append(experience_column <= max_total_years_experience)
            within_clause = and_(*within_parts)

            if experience_within_range is True:
                filters.append(within_clause)
            elif experience_within_range is False:
                outside_parts = [experience_column.is_(None)]
                if min_total_years_experience is not None:
                    outside_parts.append(experience_column < min_total_years_experience)
                if max_total_years_experience is not None:
                    outside_parts.append(experience_column > max_total_years_experience)
                filters.append(or_(*outside_parts))
            else:
                filters.append(within_clause)

        return filters
