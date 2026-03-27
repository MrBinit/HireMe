"""Add Fireflies/interview transcript columns to applicant_applications table."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.settings import get_settings


async def _run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, future=True)
    statements = [
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS interview_transcript_status VARCHAR(64)
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS interview_transcript_url VARCHAR(1000)
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS interview_transcript_summary VARCHAR(4000)
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS interview_transcript_synced_at TIMESTAMPTZ
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_applicant_applications_interview_transcript_status
        ON applicant_applications (interview_transcript_status)
        """,
    ]
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    await engine.dispose()
    print("OK: interview transcript columns ensured")


def main() -> None:
    """Entrypoint for migration helper."""

    asyncio.run(_run())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
