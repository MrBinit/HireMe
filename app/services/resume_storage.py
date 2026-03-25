"""Resume storage abstractions for local and S3 backends."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import anyio
from fastapi import UploadFile

from app.infra.s3_store import S3ObjectStore


@dataclass(frozen=True)
class StoredResume:
    """Structured result returned after a resume upload succeeds."""

    size_bytes: int
    storage_path: str


class ResumeStorage(ABC):
    """Abstract interface for storing uploaded resume files."""

    @abstractmethod
    async def save(
        self,
        *,
        resume: UploadFile,
        stored_filename: str,
        content_type: str,
        max_bytes: int,
        chunk_size: int,
    ) -> StoredResume:
        """Persist resume and return storage metadata."""

        raise NotImplementedError


class LocalResumeStorage(ResumeStorage):
    """Store resumes on local filesystem."""

    def __init__(self, resume_dir: Path):
        """Initialize local storage with base directory."""

        self._resume_dir = resume_dir

    async def save(
        self,
        *,
        resume: UploadFile,
        stored_filename: str,
        content_type: str,
        max_bytes: int,
        chunk_size: int,
    ) -> StoredResume:
        """Write resume file to local storage with size checks."""

        _ = content_type
        destination = self._resume_dir / stored_filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        total_size = 0

        try:
            async with await anyio.open_file(destination, "wb") as output_file:
                while True:
                    chunk = await resume.read(chunk_size)
                    if not chunk:
                        break

                    total_size += len(chunk)
                    if total_size > max_bytes:
                        raise ValueError("resume file exceeds maximum size")

                    await output_file.write(chunk)
        except Exception:
            if destination.exists():
                await anyio.to_thread.run_sync(os.remove, destination)
            raise

        if total_size <= 0:
            if destination.exists():
                await anyio.to_thread.run_sync(os.remove, destination)
            raise ValueError("resume file is empty")

        return StoredResume(size_bytes=total_size, storage_path=str(destination.resolve()))


class S3ResumeStorage(ResumeStorage):
    """Store resumes in S3 object storage."""

    def __init__(self, store: S3ObjectStore, bucket: str, resumes_prefix: str):
        """Initialize S3 storage with key prefix."""

        self._store = store
        self._bucket = bucket
        self._resumes_prefix = resumes_prefix.rstrip("/")

    async def save(
        self,
        *,
        resume: UploadFile,
        stored_filename: str,
        content_type: str,
        max_bytes: int,
        chunk_size: int,
    ) -> StoredResume:
        """Upload resume file to S3 and return uploaded size."""

        _ = chunk_size
        await resume.seek(0)
        key = f"{self._resumes_prefix}/{stored_filename}"
        size_bytes = await self._store.upload_fileobj(
            key=key,
            file_obj=resume.file,
            content_type=content_type,
            max_bytes=max_bytes,
        )
        return StoredResume(size_bytes=size_bytes, storage_path=f"s3://{self._bucket}/{key}")
