"""S3-backed repository for job openings."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

import anyio

from app.core.runtime_config import S3StorageRuntimeConfig
from app.infra.s3_store import S3ObjectAlreadyExistsError, S3ObjectNotFoundError, S3ObjectStore
from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.job_opening import JobOpeningCreatePayload, JobOpeningRecord


class S3JobOpeningRepository(JobOpeningRepository):
    """Store each job opening and role index as S3 JSON objects."""

    def __init__(
        self,
        store: S3ObjectStore,
        config: S3StorageRuntimeConfig,
        *,
        fetch_concurrency: int,
    ):
        """Initialize repository with object store and key-prefix config."""

        self._store = store
        self._config = config
        self._fetch_concurrency = fetch_concurrency

    async def create(self, payload: JobOpeningCreatePayload) -> JobOpeningRecord:
        """Create job opening and role-title index with conditional uniqueness."""

        now = datetime.now(tz=timezone.utc)
        record = JobOpeningRecord(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            paused=False,
            **payload.model_dump(),
        )
        role_key = self._role_index_key(record.role_title)
        record_key = self._record_key(record.id)

        try:
            await self._store.put_json(
                role_key,
                {"job_opening_id": str(record.id), "role_title": record.role_title},
                if_none_match="*",
            )
        except S3ObjectAlreadyExistsError as exc:
            raise ValueError(f"role_title '{record.role_title}' already exists") from exc

        try:
            await self._store.put_json(record_key, record.model_dump(mode="json"))
            return record
        except Exception:
            await self._store.delete(role_key)
            raise

    async def get(self, job_opening_id: UUID) -> JobOpeningRecord | None:
        """Get opening by UUID."""

        try:
            payload = await self._store.get_json(self._record_key(job_opening_id))
        except S3ObjectNotFoundError:
            return None
        return JobOpeningRecord.model_validate(self._normalize_raw_record(payload))

    async def delete(self, job_opening_id: UUID) -> bool:
        """Delete opening record and role-title index from S3."""

        opening = await self.get(job_opening_id)
        if opening is None:
            return False

        await self._store.delete(self._record_key(job_opening_id))
        await self._store.delete(self._role_index_key(opening.role_title))
        return True

    async def set_paused(self, job_opening_id: UUID, paused: bool) -> JobOpeningRecord | None:
        """Set paused state for one S3-backed opening."""

        opening = await self.get(job_opening_id)
        if opening is None:
            return None

        updated = opening.model_copy(
            update={
                "paused": paused,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        await self._store.put_json(
            self._record_key(job_opening_id),
            updated.model_dump(mode="json"),
        )
        return updated

    async def list(self, *, offset: int, limit: int) -> tuple[list[JobOpeningRecord], int]:
        """List openings from S3 objects sorted by latest creation time."""

        prefix = f"{self._config.job_openings_prefix.rstrip('/')}/"
        keys = await self._store.list_keys(prefix)
        records = await self._load_records(keys)
        records.sort(key=lambda item: item.created_at, reverse=True)

        total = len(records)
        return records[offset : offset + limit], total

    async def find_by_role_title(self, role_title: str) -> JobOpeningRecord | None:
        """Find opening via role-title index object."""

        role_key = self._role_index_key(role_title)
        try:
            payload = await self._store.get_json(role_key)
        except S3ObjectNotFoundError:
            return None

        opening_id = payload.get("job_opening_id")
        if not isinstance(opening_id, str):
            return None
        try:
            return await self.get(UUID(opening_id))
        except ValueError:
            return None

    async def list_role_titles(self) -> list[str]:
        """List role titles from index objects."""

        prefix = f"{self._config.job_opening_role_index_prefix.rstrip('/')}/"
        keys = await self._store.list_keys(prefix)
        results: list[str] = []

        async def _load(key: str) -> None:
            payload = await self._store.get_json(key)
            title = str(payload.get("role_title", "")).strip()
            if title:
                results.append(title)

        await self._run_with_limit(keys, _load)
        return results

    async def exists_role_title(self, role_title: str) -> bool:
        """Check role-title existence via index key."""

        return await self._store.exists(self._role_index_key(role_title))

    async def _load_records(self, keys: list[str]) -> list[JobOpeningRecord]:
        """Fetch and parse opening objects concurrently."""

        records: list[JobOpeningRecord] = []

        async def _load(key: str) -> None:
            payload = await self._store.get_json(key)
            records.append(JobOpeningRecord.model_validate(self._normalize_raw_record(payload)))

        await self._run_with_limit(keys, _load)
        return records

    async def _run_with_limit(self, keys: list[str], func) -> None:
        """Run async function over keys with bounded concurrency."""

        semaphore = anyio.Semaphore(self._fetch_concurrency)

        async def _worker(key: str) -> None:
            async with semaphore:
                await func(key)

        async with anyio.create_task_group() as task_group:
            for key in keys:
                task_group.start_soon(_worker, key)

    def _record_key(self, job_opening_id: UUID) -> str:
        """Return S3 key for opening record object."""

        prefix = self._config.job_openings_prefix.rstrip("/")
        return f"{prefix}/{job_opening_id}.json"

    def _role_index_key(self, role_title: str) -> str:
        """Return S3 key for role-title uniqueness index object."""

        normalized = role_title.strip().casefold().encode("utf-8")
        digest = hashlib.sha256(normalized).hexdigest()
        prefix = self._config.job_opening_role_index_prefix.rstrip("/")
        return f"{prefix}/{digest}.json"

    @staticmethod
    def _normalize_raw_record(raw_record: dict) -> dict:
        """Backfill fields for backward compatibility with older objects."""

        normalized = dict(raw_record)
        normalized.setdefault("experience_range", "0-0 years")
        normalized.setdefault("manager_email", "unknown@hireme.ai")
        created_at = normalized.get("created_at") or datetime.now(tz=timezone.utc).isoformat()
        normalized.setdefault("application_open_at", created_at)
        normalized.setdefault("application_close_at", "2100-01-01T00:00:00+00:00")
        normalized.setdefault("paused", False)
        return normalized
