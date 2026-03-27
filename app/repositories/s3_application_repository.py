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
        keyword_search: str | None = None,
        min_total_years_experience: float | None = None,
        max_total_years_experience: float | None = None,
        experience_within_range: bool | None = None,
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
            if keyword_search:
                parsed_search = (record.parsed_search_text or "").casefold()
                keyword_terms = [
                    term.casefold()
                    for term in keyword_search.split()
                    if term and len(term.strip()) >= 2
                ]
                if keyword_terms and not any(term in parsed_search for term in keyword_terms):
                    continue
            if min_total_years_experience is not None or max_total_years_experience is not None:
                years = record.parsed_total_years_experience
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

    async def get_latest_by_email(self, *, email: str) -> ApplicationRecord | None:
        """Return latest application by normalized candidate email."""

        normalized_email = email.strip().casefold()
        records, _ = await self.list(offset=0, limit=1000000)
        for record in records:
            if str(record.email).strip().casefold() == normalized_email:
                return record
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
        """Update parse fields for one S3-backed application record."""

        record = await self.get_by_id(application_id)
        if record is None:
            return False
        updated = record.model_copy(
            update={
                "parse_status": parse_status,
                "parse_result": parse_result,
                "parsed_total_years_experience": parsed_total_years_experience,
                "parsed_search_text": parsed_search_text,
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
        if "rejection_reason" in updates:
            update_payload["rejection_reason"] = updates["rejection_reason"]
        if "ai_score" in updates:
            update_payload["ai_score"] = updates["ai_score"]
        if "ai_screening_summary" in updates:
            update_payload["ai_screening_summary"] = updates["ai_screening_summary"]
        if "candidate_brief" in updates:
            update_payload["candidate_brief"] = updates["candidate_brief"]
        if "online_research_summary" in updates:
            update_payload["online_research_summary"] = updates["online_research_summary"]
        if "evaluation_status" in updates:
            update_payload["evaluation_status"] = updates["evaluation_status"]
        if "interview_schedule_status" in updates:
            update_payload["interview_schedule_status"] = updates["interview_schedule_status"]
        if "interview_schedule_options" in updates:
            update_payload["interview_schedule_options"] = updates["interview_schedule_options"]
        if "interview_schedule_sent_at" in updates:
            update_payload["interview_schedule_sent_at"] = updates["interview_schedule_sent_at"]
        if "interview_hold_expires_at" in updates:
            update_payload["interview_hold_expires_at"] = updates["interview_hold_expires_at"]
        if "interview_calendar_email" in updates:
            update_payload["interview_calendar_email"] = updates["interview_calendar_email"]
        if "interview_schedule_error" in updates:
            update_payload["interview_schedule_error"] = updates["interview_schedule_error"]
        if "interview_transcript_status" in updates:
            update_payload["interview_transcript_status"] = updates["interview_transcript_status"]
        if "interview_transcript_url" in updates:
            update_payload["interview_transcript_url"] = updates["interview_transcript_url"]
        if "interview_transcript_summary" in updates:
            update_payload["interview_transcript_summary"] = updates["interview_transcript_summary"]
        if "interview_transcript_synced_at" in updates:
            update_payload["interview_transcript_synced_at"] = updates[
                "interview_transcript_synced_at"
            ]
        if "manager_decision" in updates:
            update_payload["manager_decision"] = updates["manager_decision"]
        if "manager_decision_at" in updates:
            update_payload["manager_decision_at"] = updates["manager_decision_at"]
        if "manager_decision_note" in updates:
            update_payload["manager_decision_note"] = updates["manager_decision_note"]
        if "manager_selection_details" in updates:
            update_payload["manager_selection_details"] = updates["manager_selection_details"]
        if "manager_selection_template_output" in updates:
            update_payload["manager_selection_template_output"] = updates[
                "manager_selection_template_output"
            ]
        if "offer_letter_status" in updates:
            update_payload["offer_letter_status"] = updates["offer_letter_status"]
        if "offer_letter_storage_path" in updates:
            update_payload["offer_letter_storage_path"] = updates["offer_letter_storage_path"]
        if "offer_letter_signed_storage_path" in updates:
            update_payload["offer_letter_signed_storage_path"] = updates[
                "offer_letter_signed_storage_path"
            ]
        if "offer_letter_generated_at" in updates:
            update_payload["offer_letter_generated_at"] = updates["offer_letter_generated_at"]
        if "offer_letter_sent_at" in updates:
            update_payload["offer_letter_sent_at"] = updates["offer_letter_sent_at"]
        if "offer_letter_signed_at" in updates:
            update_payload["offer_letter_signed_at"] = updates["offer_letter_signed_at"]
        if "offer_letter_error" in updates:
            update_payload["offer_letter_error"] = updates["offer_letter_error"]
        if "docusign_envelope_id" in updates:
            update_payload["docusign_envelope_id"] = updates["docusign_envelope_id"]
        if "slack_invite_status" in updates:
            update_payload["slack_invite_status"] = updates["slack_invite_status"]
        if "slack_invited_at" in updates:
            update_payload["slack_invited_at"] = updates["slack_invited_at"]
        if "slack_user_id" in updates:
            update_payload["slack_user_id"] = updates["slack_user_id"]
        if "slack_joined_at" in updates:
            update_payload["slack_joined_at"] = updates["slack_joined_at"]
        if "slack_welcome_message" in updates:
            update_payload["slack_welcome_message"] = updates["slack_welcome_message"]
        if "slack_welcome_sent_at" in updates:
            update_payload["slack_welcome_sent_at"] = updates["slack_welcome_sent_at"]
        if "slack_onboarding_status" in updates:
            update_payload["slack_onboarding_status"] = updates["slack_onboarding_status"]
        if "slack_error" in updates:
            update_payload["slack_error"] = updates["slack_error"]

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

    async def transition_interview_schedule_status(
        self,
        *,
        application_id: UUID,
        from_statuses: set[str],
        to_status: str,
    ) -> bool:
        """Best-effort status transition for S3-backed storage."""

        record = await self.get_by_id(application_id)
        if record is None:
            return False
        if record.interview_schedule_status not in set(from_statuses):
            return False
        updated = record.model_copy(
            update={
                "interview_schedule_status": to_status,
                "interview_schedule_error": None,
            }
        )
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
