"""PostgreSQL-backed webhook idempotency repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.model.processed_webhook_event import ProcessedWebhookEvent
from app.repositories.webhook_event_dedupe_repository import (
    WebhookEventClaimResult,
    WebhookEventDedupeRepository,
)


class PostgresWebhookEventDedupeRepository(WebhookEventDedupeRepository):
    """Persist and resolve webhook idempotency state in PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Initialize repository with async SQLAlchemy session factory."""

        self._session_factory = session_factory

    async def claim_for_processing(
        self,
        *,
        event_key: str,
        source: str,
        stale_after_seconds: int,
    ) -> WebhookEventClaimResult:
        """Claim event key when new/stale; detect completed and active-in-flight duplicates."""

        now = datetime.now(tz=timezone.utc)
        stale_cutoff = now - timedelta(seconds=max(1, stale_after_seconds))
        normalized_key = event_key.strip()
        if not normalized_key:
            return "in_progress"

        async with self._session_factory() as session:
            async with session.begin():
                stmt = (
                    select(ProcessedWebhookEvent)
                    .where(ProcessedWebhookEvent.event_key == normalized_key)
                    .with_for_update()
                )
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing is None:
                    session.add(
                        ProcessedWebhookEvent(
                            event_key=normalized_key,
                            source=source,
                            status="processing",
                            attempts=1,
                            first_seen_at=now,
                            last_seen_at=now,
                            processing_started_at=now,
                            completed_at=None,
                            last_error=None,
                        )
                    )
                    return "acquired"

                existing.last_seen_at = now
                existing.attempts = int(existing.attempts or 0) + 1
                if existing.status == "completed":
                    return "already_completed"
                if (
                    existing.status == "processing"
                    and existing.processing_started_at is not None
                    and existing.processing_started_at >= stale_cutoff
                ):
                    return "in_progress"

                existing.status = "processing"
                existing.source = source
                existing.processing_started_at = now
                existing.completed_at = None
                existing.last_error = None
                return "acquired"

    async def mark_completed(self, *, event_key: str) -> None:
        """Persist completion state for one event key."""

        now = datetime.now(tz=timezone.utc)
        normalized_key = event_key.strip()
        if not normalized_key:
            return
        async with self._session_factory() as session:
            async with session.begin():
                stmt = (
                    select(ProcessedWebhookEvent)
                    .where(ProcessedWebhookEvent.event_key == normalized_key)
                    .with_for_update()
                )
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing is None:
                    session.add(
                        ProcessedWebhookEvent(
                            event_key=normalized_key,
                            source="unknown",
                            status="completed",
                            attempts=1,
                            first_seen_at=now,
                            last_seen_at=now,
                            processing_started_at=now,
                            completed_at=now,
                            last_error=None,
                        )
                    )
                    return

                existing.status = "completed"
                existing.completed_at = now
                existing.last_seen_at = now
                existing.last_error = None

    async def mark_failed(self, *, event_key: str, error: str) -> None:
        """Persist failure state for one event key without discarding retry opportunity."""

        now = datetime.now(tz=timezone.utc)
        normalized_key = event_key.strip()
        if not normalized_key:
            return
        async with self._session_factory() as session:
            async with session.begin():
                stmt = (
                    select(ProcessedWebhookEvent)
                    .where(ProcessedWebhookEvent.event_key == normalized_key)
                    .with_for_update()
                )
                existing = (await session.execute(stmt)).scalar_one_or_none()
                safe_error = error.strip()[:1000] if error else "unknown worker error"
                if existing is None:
                    session.add(
                        ProcessedWebhookEvent(
                            event_key=normalized_key,
                            source="unknown",
                            status="failed",
                            attempts=1,
                            first_seen_at=now,
                            last_seen_at=now,
                            processing_started_at=now,
                            completed_at=None,
                            last_error=safe_error,
                        )
                    )
                    return

                existing.status = "failed"
                existing.last_seen_at = now
                existing.last_error = safe_error
