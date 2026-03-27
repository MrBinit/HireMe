"""Add signed-offer S3 path column to applicant_applications table."""

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
        ADD COLUMN IF NOT EXISTS offer_letter_signed_storage_path VARCHAR(1024)
        """,
    ]
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    await engine.dispose()
    print("OK: offer_letter_signed_storage_path column ensured")


def main() -> None:
    """Entrypoint for migration helper."""

    asyncio.run(_run())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
