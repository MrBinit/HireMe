"""Tests for job opening service operations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.runtime_config import JobOpeningRuntimeConfig
from app.repositories.local_job_opening_repository import LocalJobOpeningRepository
from app.schemas.job_opening import JobOpeningCreatePayload
from app.services.job_opening_service import JobOpeningService


def test_delete_job_opening_by_id(tmp_path: Path) -> None:
    """Created job opening should be deletable by its UUID."""

    async def run() -> None:
        repo = LocalJobOpeningRepository(tmp_path / "job_openings.json")
        service = JobOpeningService(
            repository=repo,
            config=JobOpeningRuntimeConfig(),
        )

        opening = await service.create(
            JobOpeningCreatePayload(
                role_title="Delete Me Engineer",
                team="Platform",
                location="remote",
                experience_level="mid",
                experience_range="2-3 years",
                application_open_at=datetime.now(tz=timezone.utc) - timedelta(days=1),
                application_close_at=datetime.now(tz=timezone.utc) + timedelta(days=7),
                responsibilities=["Build APIs"],
                requirements=["Python"],
            )
        )

        deleted = await service.delete(str(opening.id))
        deleted_again = await service.delete(str(opening.id))

        assert deleted is True
        assert deleted_again is False

    asyncio.run(run())


def test_job_opening_status_open_or_closed(tmp_path: Path) -> None:
    """Service should expose runtime status as open/closed on returned openings."""

    async def run() -> None:
        repo = LocalJobOpeningRepository(tmp_path / "job_openings.json")
        service = JobOpeningService(
            repository=repo,
            config=JobOpeningRuntimeConfig(),
        )

        open_record = await service.create(
            JobOpeningCreatePayload(
                role_title="Open Engineer",
                team="Platform",
                location="remote",
                experience_level="mid",
                experience_range="2-3 years",
                application_open_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
                application_close_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
                responsibilities=["Build APIs"],
                requirements=["Python"],
            )
        )
        closed_record = await service.create(
            JobOpeningCreatePayload(
                role_title="Closed Engineer",
                team="Platform",
                location="remote",
                experience_level="mid",
                experience_range="2-3 years",
                application_open_at=datetime.now(tz=timezone.utc) - timedelta(days=2),
                application_close_at=datetime.now(tz=timezone.utc) - timedelta(days=1),
                responsibilities=["Build APIs"],
                requirements=["Python"],
            )
        )

        listed = await service.list(offset=0, limit=10)
        by_role = {item.role_title: item.status for item in listed.items}

        assert open_record.status == "open"
        assert closed_record.status == "closed"
        assert by_role["Open Engineer"] == "open"
        assert by_role["Closed Engineer"] == "closed"

    asyncio.run(run())


def test_pause_job_opening_sets_status_paused(tmp_path: Path) -> None:
    """Paused openings should expose paused status in service responses."""

    async def run() -> None:
        repo = LocalJobOpeningRepository(tmp_path / "job_openings.json")
        service = JobOpeningService(
            repository=repo,
            config=JobOpeningRuntimeConfig(),
        )

        opening = await service.create(
            JobOpeningCreatePayload(
                role_title="Pause Target Engineer",
                team="Platform",
                location="remote",
                experience_level="mid",
                experience_range="2-3 years",
                application_open_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
                application_close_at=datetime.now(tz=timezone.utc) + timedelta(days=7),
                responsibilities=["Build APIs"],
                requirements=["Python"],
            )
        )
        assert opening.status == "open"

        paused = await service.set_paused(str(opening.id), True)
        assert paused is not None
        assert paused.status == "paused"
        assert paused.paused is True

    asyncio.run(run())
