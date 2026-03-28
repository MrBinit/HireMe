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
    """Create ORM tables in the target DB if they do not already exist.

    Schema changes for existing PostgreSQL databases must be applied via Alembic
    migrations. Startup intentionally avoids running schema patch DDL/DML.
    """

    engine = get_async_engine(config)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
