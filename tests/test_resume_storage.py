"""Tests for resume storage abstractions."""

from __future__ import annotations

import asyncio
import io

from starlette.datastructures import Headers, UploadFile

from app.services.resume_storage import S3ResumeStorage


class _FakeS3Store:
    """Simple fake store for unit testing S3 resume storage behavior."""

    async def upload_fileobj(
        self,
        *,
        key: str,
        file_obj,
        content_type: str,
        max_bytes: int,
    ) -> int:
        """Pretend to upload and return size after enforcing max-bytes rule."""

        _ = key
        _ = content_type
        file_obj.seek(0)
        payload = file_obj.read()
        size = len(payload)
        if size > max_bytes:
            raise ValueError("resume file exceeds maximum size")
        return size


def test_s3_resume_storage_returns_s3_uri_path() -> None:
    """S3 resume storage should return a concrete s3:// storage path."""

    async def run() -> None:
        storage = S3ResumeStorage(
            store=_FakeS3Store(),
            bucket="hireme-cv-bucket",
            resumes_prefix="hireme/resumes",
        )
        upload = UploadFile(
            file=io.BytesIO(b"%PDF-1.4\nsample"),
            filename="resume.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )
        result = await storage.save(
            resume=upload,
            stored_filename="abc123.pdf",
            content_type="application/pdf",
            max_bytes=1024,
            chunk_size=1024,
        )
        assert result.size_bytes > 0
        assert result.storage_path == "s3://hireme-cv-bucket/hireme/resumes/abc123.pdf"

    asyncio.run(run())
