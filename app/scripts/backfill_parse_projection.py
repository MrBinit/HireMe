"""Backfill denormalized parse summary columns from existing parse_result JSON."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.runtime_config import get_runtime_config
from app.infra.database import get_async_session_factory
from app.model.applicant_application import ApplicantApplication
from app.repositories.application_repository import extract_parse_projection


async def main() -> None:
    """Populate parse summary columns for existing applicant rows."""

    runtime_config = get_runtime_config()
    session_factory = get_async_session_factory(runtime_config.postgres)

    async with session_factory() as session:
        result = await session.execute(select(ApplicantApplication))
        rows = list(result.scalars().all())

        updated = 0
        for row in rows:
            projection = extract_parse_projection(row.parse_result)
            row.latest_position = projection["latest_position"]
            row.total_years_experience = projection["total_years_experience"]
            row.parsed_skills = projection["parsed_skills"]
            row.parsed_education = projection["parsed_education"]
            updated += 1

        await session.commit()
        print(f"Backfill completed. Rows updated: {updated}")


if __name__ == "__main__":
    asyncio.run(main())
