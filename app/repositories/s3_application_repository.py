"""S3-backed repository for candidate applications."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.core.runtime_config import S3StorageRuntimeConfig
from app.infra.s3_store import S3ObjectAlreadyExistsError, S3ObjectStore
from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
    extract_parse_projection,
)
from app.schemas.application import ApplicationRecord
from app.schemas.application import ApplicantStatus
from app.schemas.application import ParseStatus


class S3ApplicationRepository(ApplicationRepository):
    """Store application records and dedupe markers in S3."""

    def __init__(self, store: S3ObjectStore, config: S3StorageRuntimeConfig):
        """Initialize repository with object store and S3 key config."""

        self._store = store
        self._config = config

    async def create(self, record: ApplicationRecord) -> ApplicationRecord:
        """Persist application while enforcing email+opening uniqueness."""

        dedupe_key = self._dedupe_key(
            email=record.email,
            job_opening_id=record.job_opening_id,
        )
        application_key = self._application_key(record.id)

        try:
            await self._store.put_json(
                dedupe_key,
                {
                    "job_opening_id": str(record.job_opening_id),
                    "email": record.email,
                    "application_id": str(record.id),
                },
                if_none_match="*",
            )
        except S3ObjectAlreadyExistsError as exc:
            raise DuplicateApplicationError(
                "application already exists for this email and job opening"
            ) from exc

        try:
            await self._store.put_json(application_key, record.model_dump(mode="json"))
            return record
        except Exception:
            await self._store.delete(dedupe_key)
            raise

    async def exists_for_email_and_opening(self, *, email: str, job_opening_id: UUID) -> bool:
        """Return True when dedupe marker exists for email+opening."""

        return await self._store.exists(
            self._dedupe_key(email=email, job_opening_id=job_opening_id)
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
    ) -> tuple[list[ApplicationRecord], int]:
        """List application records persisted in S3."""

        prefix = f"{self._config.applications_prefix.rstrip('/')}/"
        keys = await self._store.list_keys(prefix)
        records: list[ApplicationRecord] = []
        for key in keys:
            payload = await self._store.get_json(key)
            record = ApplicationRecord.model_validate(payload)
            if job_opening_id is not None and record.job_opening_id != job_opening_id:
                continue
            if (
                role_selection is not None
                and record.role_selection.strip().casefold() != role_selection.strip().casefold()
            ):
                continue
            if applicant_status is not None and record.applicant_status != applicant_status:
                continue
            if submitted_from is not None and record.created_at < submitted_from:
                continue
            if submitted_to is not None and record.created_at > submitted_to:
                continue
            records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        total = len(records)
        return records[offset : offset + limit], total

    async def get_by_id(self, application_id: UUID) -> ApplicationRecord | None:
        """Return one application record by id."""

        key = self._application_key(application_id)
        if not await self._store.exists(key):
            return None
        payload = await self._store.get_json(key)
        return ApplicationRecord.model_validate(payload)

    async def update_parse_state(
        self,
        *,
        application_id: UUID,
        parse_status: ParseStatus,
        parse_result: dict | None,
    ) -> bool:
        """Update parse fields for one S3-backed application record."""

        record = await self.get_by_id(application_id)
        if record is None:
            return False
        projection = extract_parse_projection(parse_result)
        updated = record.model_copy(
            update={
                "parse_status": parse_status,
                "parse_result": parse_result,
                "latest_position": projection["latest_position"],
                "total_years_experience": projection["total_years_experience"],
                "parsed_skills": projection["parsed_skills"],
                "parsed_education": projection["parsed_education"],
            }
        )
        await self._store.put_json(
            self._application_key(application_id),
            updated.model_dump(mode="json"),
        )
        return True

    async def update_reference_status(
        self,
        *,
        application_id: UUID,
        reference_status: bool,
    ) -> bool:
        """Update reference status for one S3-backed application record."""

        record = await self.get_by_id(application_id)
        if record is None:
            return False
        updated = record.model_copy(update={"reference_status": reference_status})
        await self._store.put_json(
            self._application_key(application_id),
            updated.model_dump(mode="json"),
        )
        return True

    async def update_applicant_status(
        self,
        *,
        application_id: UUID,
        applicant_status: ApplicantStatus,
        note: str | None = None,
    ) -> bool:
        """Update applicant lifecycle status for one S3-backed record."""

        return await self.update_admin_review(
            application_id=application_id,
            updates={"applicant_status": applicant_status, "note": note},
        )

    async def update_admin_review(
        self,
        *,
        application_id: UUID,
        updates: dict[str, Any],
    ) -> bool:
        """Update admin-review fields on one S3-backed record."""

        record = await self.get_by_id(application_id)
        if record is None:
            return False

        update_payload: dict[str, Any] = {}
        if "applicant_status" in updates and updates["applicant_status"] is not None:
            update_payload["applicant_status"] = updates["applicant_status"]
        if "ai_score" in updates:
            update_payload["ai_score"] = updates["ai_score"]
        if "ai_screening_summary" in updates:
            update_payload["ai_screening_summary"] = updates["ai_screening_summary"]
        if "online_research_summary" in updates:
            update_payload["online_research_summary"] = updates["online_research_summary"]

        note = updates.get("note")
        if "applicant_status" in updates and updates["applicant_status"] is not None or note:
            history = list(record.status_history or [])
            history.append(
                {
                    "status": update_payload.get("applicant_status", record.applicant_status),
                    "note": note,
                    "changed_at": datetime.now(tz=timezone.utc)
                    .isoformat()
                    .replace(
                        "+00:00",
                        "Z",
                    ),
                    "source": "admin",
                }
            )
            update_payload["status_history"] = history

        updated = record.model_copy(update=update_payload)
        await self._store.put_json(
            self._application_key(application_id),
            updated.model_dump(mode="json"),
        )
        return True

    def _application_key(self, application_id: UUID) -> str:
        """Return S3 key for application record object."""

        prefix = self._config.applications_prefix.rstrip("/")
        return f"{prefix}/{application_id}.json"

    def _dedupe_key(self, *, email: str, job_opening_id: UUID) -> str:
        """Return dedupe marker key for email+opening tuple."""

        normalized_email = email.strip().casefold().encode("utf-8")
        email_hash = hashlib.sha256(normalized_email).hexdigest()
        prefix = self._config.application_dedupe_prefix.rstrip("/")
        return f"{prefix}/{job_opening_id}/{email_hash}.json"
