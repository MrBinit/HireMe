"""Async SQLAlchemy engine/session utilities for PostgreSQL-backed metadata."""

from __future__ import annotations

import ssl
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql import text

from app.core.runtime_config import PostgresRuntimeConfig
from app.core.settings import get_settings
from app.model import Base

_ENGINE: AsyncEngine | None = None
_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None


def _normalize_database_url(url: str) -> str:
    """Normalize DB URL to an async SQLAlchemy dialect URL."""

    value = url.strip()
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


def get_async_engine(config: PostgresRuntimeConfig) -> AsyncEngine:
    """Return cached async engine for metadata persistence."""

    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    settings = get_settings()
    database_url = _normalize_database_url(settings.database_url)
    if database_url.startswith("sqlite+"):
        _ENGINE = create_async_engine(
            database_url,
            echo=config.echo_sql,
        )
    else:
        connect_args = {
            "timeout": config.connect_timeout_seconds,
            "command_timeout": config.command_timeout_seconds,
        }
        if config.ssl_mode != "disable":
            if config.ssl_root_cert_path:
                ca_path = Path(config.ssl_root_cert_path).expanduser()
                ssl_context = ssl.create_default_context(cafile=str(ca_path))
            else:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            connect_args["ssl"] = ssl_context

        _ENGINE = create_async_engine(
            database_url,
            echo=config.echo_sql,
            pool_pre_ping=config.pool_pre_ping,
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout_seconds,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=connect_args,
        )
    return _ENGINE


def get_async_session_factory(
    config: PostgresRuntimeConfig,
) -> async_sessionmaker[AsyncSession]:
    """Return cached session factory bound to the async engine."""

    global _SESSION_FACTORY
    if _SESSION_FACTORY is not None:
        return _SESSION_FACTORY

    _SESSION_FACTORY = async_sessionmaker(
        bind=get_async_engine(config),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    return _SESSION_FACTORY


async def init_db_schema(config: PostgresRuntimeConfig) -> None:
    """Create ORM tables in the target DB if they do not already exist."""

    engine = get_async_engine(config)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if connection.dialect.name == "postgresql":
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS reference_status BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS ai_score DOUBLE PRECISION"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS ai_screening_summary VARCHAR(4000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS candidate_brief VARCHAR(1500)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS online_research_summary VARCHAR(4000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS interview_schedule_status VARCHAR(30)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ALTER COLUMN interview_schedule_status TYPE VARCHAR(64)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS interview_schedule_options JSONB"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS interview_schedule_sent_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS interview_hold_expires_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS interview_calendar_email VARCHAR(320)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS interview_schedule_error VARCHAR(1000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS manager_decision VARCHAR(16)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS manager_decision_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS manager_decision_note VARCHAR(1000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS manager_selection_details JSONB"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS manager_selection_template_output VARCHAR(8000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS offer_letter_status VARCHAR(32)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS offer_letter_storage_path VARCHAR(1024)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS offer_letter_signed_storage_path VARCHAR(1024)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS offer_letter_generated_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS offer_letter_sent_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS offer_letter_signed_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS offer_letter_error VARCHAR(1000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS docusign_envelope_id VARCHAR(128)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_invite_status VARCHAR(64)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_invited_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_user_id VARCHAR(64)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_joined_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_welcome_message VARCHAR(4000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_welcome_sent_at TIMESTAMPTZ"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_onboarding_status VARCHAR(64)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS slack_error VARCHAR(1000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS status_history JSONB NOT NULL DEFAULT '[]'::jsonb"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS parsed_total_years_experience DOUBLE PRECISION"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS parsed_search_text VARCHAR(8000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR(1000)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ADD COLUMN IF NOT EXISTS evaluation_status VARCHAR(30)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications " "ALTER COLUMN portfolio_url DROP NOT NULL"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "ALTER COLUMN applicant_status SET DEFAULT 'applied'"
                )
            )
            await connection.execute(
                text("ALTER TABLE applicant_applications " "DROP COLUMN IF EXISTS latest_position")
            )
            await connection.execute(
                text(
                    "ALTER TABLE applicant_applications "
                    "DROP COLUMN IF EXISTS total_years_experience"
                )
            )
            await connection.execute(
                text("ALTER TABLE applicant_applications " "DROP COLUMN IF EXISTS parsed_skills")
            )
            await connection.execute(
                text("ALTER TABLE applicant_applications " "DROP COLUMN IF EXISTS parsed_education")
            )
            await connection.execute(
                text(
                    "ALTER TABLE job_openings "
                    "ADD COLUMN IF NOT EXISTS paused BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE job_openings "
                    "ADD COLUMN IF NOT EXISTS manager_email VARCHAR(320) "
                    "NOT NULL DEFAULT 'unknown@hireme.ai'"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE job_openings "
                    "ALTER COLUMN manager_email SET DEFAULT 'unknown@hireme.ai'"
                )
            )
            await connection.execute(
                text(
                    "UPDATE job_openings "
                    "SET manager_email = 'unknown@hireme.ai' "
                    "WHERE manager_email = 'unknown@hireme.local'"
                )
            )
            await connection.execute(
                text(
                    "UPDATE applicant_applications "
                    "SET interview_schedule_status = 'interview_options_sent' "
                    "WHERE interview_schedule_status IN ('options_sent', 'interview_email_sent')"
                )
            )
            await connection.execute(text("DROP INDEX IF EXISTS idx_applications_latest_position"))
            await connection.execute(
                text("DROP INDEX IF EXISTS idx_applications_total_years_experience")
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_status "
                    "ON applicant_applications (applicant_status)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_evaluation_status "
                    "ON applicant_applications (evaluation_status)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_created_at "
                    "ON applicant_applications (created_at)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_interview_schedule_status "
                    "ON applicant_applications (interview_schedule_status)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_manager_decision "
                    "ON applicant_applications (manager_decision)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_offer_letter_status "
                    "ON applicant_applications (offer_letter_status)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_docusign_envelope_id "
                    "ON applicant_applications (docusign_envelope_id)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_slack_invite_status "
                    "ON applicant_applications (slack_invite_status)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_slack_user_id "
                    "ON applicant_applications (slack_user_id)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_slack_onboarding_status "
                    "ON applicant_applications (slack_onboarding_status)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_role "
                    "ON applicant_applications (role_selection)"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_applications_parsed_total_years_experience "
                    "ON applicant_applications (parsed_total_years_experience)"
                )
            )
