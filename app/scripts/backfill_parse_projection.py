"""Backfill parse projection columns from parse_result JSON."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import select

from app.core.runtime_config import get_runtime_config
from app.infra.database import get_async_session_factory
from app.model.applicant_application import ApplicantApplication


def _build_search_text(parse_result: dict[str, Any], *, max_chars: int) -> str:
    """Build normalized search text from parse-result sections."""

    segments: list[str] = []
    for key in ["skills", "old_offices", "key_achievements"]:
        values = parse_result.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, str):
                segments.append(item)

    education = parse_result.get("education")
    if isinstance(education, list):
        for row in education:
            if not isinstance(row, dict):
                continue
            for value in row.values():
                if isinstance(value, str):
                    segments.append(value)

    work_experience = parse_result.get("work_experience")
    if isinstance(work_experience, list):
        for row in work_experience:
            if not isinstance(row, dict):
                continue
            for key in ["company", "position"]:
                value = row.get(key)
                if isinstance(value, str):
                    segments.append(value)
            job_description = row.get("job_description")
            if isinstance(job_description, list):
                for item in job_description:
                    if isinstance(item, str):
                        segments.append(item)

    compact = " ".join(segments).casefold()
    normalized = " ".join(re.findall(r"[a-z0-9\+\#\.]{2,}", compact))
    return normalized[:max_chars]


def _extract_total_years(parse_result: dict[str, Any]) -> float | None:
    """Extract total years of experience from parse_result when available."""

    value = parse_result.get("total_years_experience")
    if isinstance(value, (int, float)):
        return float(value)
    return None


async def main() -> None:
    """Populate parsed projection columns for rows that already have parse_result."""

    runtime_config = get_runtime_config()
    session_factory = get_async_session_factory(runtime_config.postgres)
    max_chars = runtime_config.application.prefilter_max_search_text_chars

    updated = 0
    async with session_factory() as session:
        result = await session.execute(select(ApplicantApplication))
        rows = list(result.scalars().all())
        for row in rows:
            if not isinstance(row.parse_result, dict):
                continue
            row.parsed_total_years_experience = _extract_total_years(row.parse_result)
            row.parsed_search_text = _build_search_text(row.parse_result, max_chars=max_chars)
            updated += 1
        await session.commit()

    print(f"Backfill complete. Updated rows: {updated}")


def _run_main() -> None:
    """Synchronous wrapper for async script entrypoint."""

    asyncio.run(main())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(_run_main))
