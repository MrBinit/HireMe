"""Local JSON-backed repository for application records."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
    extract_parse_projection,
)
from app.schemas.application import ApplicationRecord
from app.schemas.application import ApplicantStatus
from app.schemas.application import ParseStatus


class LocalApplicationRepository(ApplicationRepository):
    """Persist applications into a local JSON file."""

    def __init__(self, storage_file: Path):
        """Initialize repository with target storage path."""

        self._storage_file = storage_file
        self._lock = asyncio.Lock()

    async def create(self, record: ApplicationRecord) -> ApplicationRecord:
        """Append an application record to local JSON storage."""

        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            if self._has_duplicate(
                raw_records=raw_records,
                email=record.email,
                job_opening_id=record.job_opening_id,
            ):
                raise DuplicateApplicationError(
                    "application already exists for this email and job opening"
                )
            raw_records.append(record.model_dump(mode="json"))
            await asyncio.to_thread(self._write_records_sync, raw_records)
        return record

    async def exists_for_email_and_opening(self, *, email: str, job_opening_id: UUID) -> bool:
        """Check whether an email has already applied to a specific opening."""

        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            return self._has_duplicate(
                raw_records=raw_records,
                email=email,
                job_opening_id=job_opening_id,
            )

    async def list(
        self,
        *,
        offset: int,
        limit: int,
        job_opening_id: UUID | None = None,
    ) -> tuple[list[ApplicationRecord], int]:
        """Return paginated local applications, optionally filtered by opening."""

        raw_records = await asyncio.to_thread(self._read_records_sync)
        filtered: list[dict] = []
        target_id = str(job_opening_id) if job_opening_id is not None else None
        for item in raw_records:
            if target_id is None or str(item.get("job_opening_id", "")) == target_id:
                filtered.append(item)
        parsed = [ApplicationRecord.model_validate(item) for item in filtered]
        parsed.sort(key=lambda record: record.created_at, reverse=True)
        total = len(parsed)
        return parsed[offset : offset + limit], total

    async def get_by_id(self, application_id: UUID) -> ApplicationRecord | None:
        """Return one local application by id."""

        raw_records = await asyncio.to_thread(self._read_records_sync)
        target_id = str(application_id)
        for raw_record in raw_records:
            if str(raw_record.get("id", "")) == target_id:
                return ApplicationRecord.model_validate(raw_record)
        return None

    async def update_parse_state(
        self,
        *,
        application_id: UUID,
        parse_status: ParseStatus,
        parse_result: dict | None,
    ) -> bool:
        """Update parse fields for a local application."""

        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            target_id = str(application_id)

            for item in raw_records:
                if str(item.get("id", "")) != target_id:
                    continue
                item["parse_status"] = parse_status
                item["parse_result"] = parse_result
                projection = extract_parse_projection(parse_result)
                item["latest_position"] = projection["latest_position"]
                item["total_years_experience"] = projection["total_years_experience"]
                item["parsed_skills"] = projection["parsed_skills"]
                item["parsed_education"] = projection["parsed_education"]
                await asyncio.to_thread(self._write_records_sync, raw_records)
                return True

            return False

    async def update_reference_status(
        self,
        *,
        application_id: UUID,
        reference_status: bool,
    ) -> bool:
        """Update reference status for a local application."""

        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            target_id = str(application_id)

            for item in raw_records:
                if str(item.get("id", "")) != target_id:
                    continue
                item["reference_status"] = reference_status
                await asyncio.to_thread(self._write_records_sync, raw_records)
                return True

            return False

    async def update_applicant_status(
        self,
        *,
        application_id: UUID,
        applicant_status: ApplicantStatus,
    ) -> bool:
        """Update applicant lifecycle status for one local application."""

        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            target_id = str(application_id)

            for item in raw_records:
                if str(item.get("id", "")) != target_id:
                    continue
                item["applicant_status"] = applicant_status
                await asyncio.to_thread(self._write_records_sync, raw_records)
                return True

            return False

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

    def _write_records_sync(self, raw_records: list[dict]) -> None:
        """Write raw records using atomic file replacement."""

        self._storage_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._storage_file.with_suffix(".tmp")

        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump({"items": raw_records}, handle, indent=2)

        temp_path.replace(self._storage_file)

    @staticmethod
    def _normalize_email(email: str) -> str:
        """Normalize email string for duplicate checks."""

        return email.strip().casefold()

    def _has_duplicate(
        self,
        *,
        raw_records: list[dict],
        email: str,
        job_opening_id: UUID,
    ) -> bool:
        """Return True when matching email+opening exists in stored records."""

        normalized_email = self._normalize_email(email)
        target_job_opening_id = str(job_opening_id)
        for raw_record in raw_records:
            existing_email = self._normalize_email(str(raw_record.get("email", "")))
            existing_job_opening_id = str(raw_record.get("job_opening_id", ""))
            if (
                existing_email == normalized_email
                and existing_job_opening_id == target_job_opening_id
            ):
                return True
        return False
