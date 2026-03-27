"""Add Slack onboarding columns to applicant_applications table."""

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
        ADD COLUMN IF NOT EXISTS slack_invite_status VARCHAR(64)
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS slack_invited_at TIMESTAMPTZ
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS slack_user_id VARCHAR(64)
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS slack_joined_at TIMESTAMPTZ
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS slack_welcome_message VARCHAR(4000)
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS slack_welcome_sent_at TIMESTAMPTZ
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS slack_onboarding_status VARCHAR(64)
        """,
        """
        ALTER TABLE applicant_applications
        ADD COLUMN IF NOT EXISTS slack_error VARCHAR(1000)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_applicant_applications_slack_invite_status
        ON applicant_applications (slack_invite_status)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_applicant_applications_slack_user_id
        ON applicant_applications (slack_user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_applicant_applications_slack_onboarding_status
        ON applicant_applications (slack_onboarding_status)
        """,
    ]
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    await engine.dispose()
    print("OK: slack columns ensured")


def main() -> None:
    """Entrypoint for migration helper."""

    asyncio.run(_run())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))

