"""Local JSON-backed repository for job openings."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.job_opening import JobOpeningCreatePayload, JobOpeningRecord


class LocalJobOpeningRepository(JobOpeningRepository):
    """Persist job openings into a local JSON file."""

    def __init__(self, storage_file: Path):
        """Initialize repository with target storage path."""

        self._storage_file = storage_file
        self._lock = asyncio.Lock()

    async def create(self, payload: JobOpeningCreatePayload) -> JobOpeningRecord:
        """Create and store a new job opening record."""

        now = datetime.now(tz=timezone.utc)
        record = JobOpeningRecord(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            **payload.model_dump(),
        )
        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            raw_records.append(record.model_dump(mode="json"))
            await asyncio.to_thread(self._write_records_sync, raw_records)
        return record

    async def get(self, job_opening_id: UUID) -> JobOpeningRecord | None:
        """Find opening by UUID."""

        raw_records = await asyncio.to_thread(self._read_records_sync)
        target_id = str(job_opening_id)
        for raw_record in raw_records:
            if str(raw_record.get("id", "")) == target_id:
                return JobOpeningRecord.model_validate(self._normalize_raw_record(raw_record))
        return None

    async def delete(self, job_opening_id: UUID) -> bool:
        """Delete opening by UUID from local storage."""

        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            target_id = str(job_opening_id)
            filtered = [item for item in raw_records if str(item.get("id", "")) != target_id]
            if len(filtered) == len(raw_records):
                return False
            await asyncio.to_thread(self._write_records_sync, filtered)
            return True

    async def list(self, *, offset: int, limit: int) -> tuple[list[JobOpeningRecord], int]:
        """Return paginated job opening records sorted by latest created."""

        raw_records = await asyncio.to_thread(self._read_records_sync)
        parsed_records = [
            JobOpeningRecord.model_validate(self._normalize_raw_record(item))
            for item in raw_records
        ]
        parsed_records.sort(key=lambda item: item.created_at, reverse=True)

        total = len(parsed_records)
        return parsed_records[offset : offset + limit], total

    async def find_by_role_title(self, role_title: str) -> JobOpeningRecord | None:
        """Find opening by role title using case-insensitive matching."""

        target = role_title.strip().casefold()
        raw_records = await asyncio.to_thread(self._read_records_sync)
        for raw_record in raw_records:
            candidate = str(raw_record.get("role_title", "")).strip().casefold()
            if candidate == target:
                return JobOpeningRecord.model_validate(self._normalize_raw_record(raw_record))
        return None

    async def list_role_titles(self) -> list[str]:
        """Return all role titles in insertion order."""

        raw_records = await asyncio.to_thread(self._read_records_sync)
        titles: list[str] = []
        for raw_record in raw_records:
            title = str(raw_record.get("role_title", "")).strip()
            if title:
                titles.append(title)
        return titles

    async def exists_role_title(self, role_title: str) -> bool:
        """Check whether a role title already exists."""

        return await self.find_by_role_title(role_title) is not None

    def _read_records_sync(self) -> list[dict]:
        """Read raw JSON records from disk."""

        if not self._storage_file.exists():
            return []

        with self._storage_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        if isinstance(payload, dict):
            items = payload.get("items", [])
            return items if isinstance(items, list) else []
        return []

    @staticmethod
    def _normalize_raw_record(raw_record: dict) -> dict:
        """Backfill fields for backward compatibility with older local data."""

        normalized = dict(raw_record)
        normalized.setdefault("experience_range", "0-0 years")
        created_at = normalized.get("created_at") or datetime.now(tz=timezone.utc).isoformat()
        normalized.setdefault("application_open_at", created_at)
        normalized.setdefault("application_close_at", "2100-01-01T00:00:00+00:00")
        return normalized

    def _write_records_sync(self, raw_records: list[dict]) -> None:
        """Write raw records using atomic file replacement."""

        self._storage_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._storage_file.with_suffix(".tmp")

        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump({"items": raw_records}, handle, indent=2)

        temp_path.replace(self._storage_file)
