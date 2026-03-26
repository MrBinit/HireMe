"""Local JSON-backed repository for application records."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
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
        role_selection: str | None = None,
        applicant_status: ApplicantStatus | None = None,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        keyword_search: str | None = None,
        min_total_years_experience: float | None = None,
        max_total_years_experience: float | None = None,
        experience_within_range: bool | None = None,
    ) -> tuple[list[ApplicationRecord], int]:
        """Return paginated local applications, optionally filtered by opening."""

        raw_records = await asyncio.to_thread(self._read_records_sync)
        filtered: list[dict] = []
        target_id = str(job_opening_id) if job_opening_id is not None else None
        for item in raw_records:
            if target_id is not None and str(item.get("job_opening_id", "")) != target_id:
                continue
            if role_selection is not None:
                role_value = str(item.get("role_selection", "")).strip().casefold()
                if role_value != role_selection.strip().casefold():
                    continue
            if applicant_status is not None and item.get("applicant_status") != applicant_status:
                continue
            created_at_raw = item.get("created_at")
            created_at = None
            if isinstance(created_at_raw, str):
                try:
                    created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                except ValueError:
                    created_at = None
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if (
                submitted_from is not None
                and created_at is not None
                and created_at < submitted_from
            ):
                continue
            if submitted_to is not None and created_at is not None and created_at > submitted_to:
                continue

            parsed_search_text = str(item.get("parsed_search_text") or "").casefold()
            if keyword_search:
                keyword_terms = [
                    term.casefold()
                    for term in keyword_search.split()
                    if term and len(term.strip()) >= 2
                ]
                if keyword_terms and not any(term in parsed_search_text for term in keyword_terms):
                    continue

            years_value = item.get("parsed_total_years_experience")
            years: float | None = None
            if isinstance(years_value, (int, float)):
                years = float(years_value)

            if min_total_years_experience is not None or max_total_years_experience is not None:
                is_within = True
                if years is None:
                    is_within = False
                if (
                    years is not None
                    and min_total_years_experience is not None
                    and years < min_total_years_experience
                ):
                    is_within = False
                if (
                    years is not None
                    and max_total_years_experience is not None
                    and years > max_total_years_experience
                ):
                    is_within = False
                if experience_within_range is True and not is_within:
                    continue
                if experience_within_range is False and is_within:
                    continue
                if experience_within_range is None and not is_within:
                    continue

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
        parsed_total_years_experience: float | None = None,
        parsed_search_text: str | None = None,
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
                item["parsed_total_years_experience"] = parsed_total_years_experience
                item["parsed_search_text"] = parsed_search_text
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
        note: str | None = None,
    ) -> bool:
        """Update applicant lifecycle status for one local application."""

        return await self.update_admin_review(
            application_id=application_id,
            updates={
                "applicant_status": applicant_status,
                "note": note,
            },
        )

    async def update_admin_review(
        self,
        *,
        application_id: UUID,
        updates: dict[str, Any],
    ) -> bool:
        """Update admin-review fields for one local application."""

        async with self._lock:
            raw_records = await asyncio.to_thread(self._read_records_sync)
            target_id = str(application_id)

            for item in raw_records:
                if str(item.get("id", "")) != target_id:
                    continue

                now_iso = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
                if "applicant_status" in updates and updates["applicant_status"] is not None:
                    item["applicant_status"] = updates["applicant_status"]
                if "rejection_reason" in updates:
                    item["rejection_reason"] = updates["rejection_reason"]
                if "evaluation_status" in updates:
                    item["evaluation_status"] = updates["evaluation_status"]

                if "ai_score" in updates:
                    item["ai_score"] = updates["ai_score"]
                if "ai_screening_summary" in updates:
                    item["ai_screening_summary"] = updates["ai_screening_summary"]
                if "candidate_brief" in updates:
                    item["candidate_brief"] = updates["candidate_brief"]
                if "online_research_summary" in updates:
                    item["online_research_summary"] = updates["online_research_summary"]

                note = updates.get("note")
                if (
                    "applicant_status" in updates and updates["applicant_status"] is not None
                ) or note:
                    history = item.get("status_history")
                    if not isinstance(history, list):
                        history = []
                    history.append(
                        {
                            "status": item.get("applicant_status"),
                            "note": note,
                            "changed_at": now_iso,
                            "source": "admin",
                        }
                    )
                    item["status_history"] = history

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
