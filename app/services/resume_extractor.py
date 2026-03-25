"""Resume text extraction using LangChain community loaders."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import anyio

from app.infra.s3_store import S3ObjectStore


class ResumeExtractionError(RuntimeError):
    """Raised when resume text extraction fails."""


class LangChainResumeExtractor:
    """Extract text from PDF/DOCX resumes using UnstructuredFileLoader."""

    def __init__(self, *, s3_store: S3ObjectStore, max_extracted_chars: int):
        """Initialize extractor with S3 store and truncation guard."""

        self._s3_store = s3_store
        self._max_extracted_chars = max(1000, max_extracted_chars)

    async def extract_text(self, storage_path: str) -> str:
        """Extract plain text from resume storage path."""

        suffix = Path(storage_path).suffix.lower()
        if storage_path.startswith("s3://"):
            bucket, key = self._parse_s3_uri(storage_path)
            content = await self._s3_store.get_bytes(key, bucket=bucket)
            if not suffix:
                suffix = Path(key).suffix.lower()
        else:
            content = await anyio.to_thread.run_sync(lambda: Path(storage_path).read_bytes())

        if suffix not in {".pdf", ".docx"}:
            raise ResumeExtractionError("resume extraction supports only PDF and DOCX")

        text = await anyio.to_thread.run_sync(
            self._extract_with_unstructured_loader,
            content,
            suffix,
        )
        normalized_lines = [" ".join(line.split()).strip() for line in text.splitlines()]
        normalized = "\n".join(line for line in normalized_lines if line)
        if not normalized:
            raise ResumeExtractionError("no extractable text found in resume")

        clipped = normalized[: self._max_extracted_chars]
        return clipped.strip() + self._clip_suffix(normalized, clipped)

    @staticmethod
    def _parse_s3_uri(storage_path: str) -> tuple[str, str]:
        """Parse `s3://bucket/key` into bucket/key tuple."""

        parsed = urlparse(storage_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise ResumeExtractionError("invalid s3 resume path")
        return bucket, key

    @staticmethod
    def _extract_with_unstructured_loader(content: bytes, suffix: str) -> str:
        """Run UnstructuredFileLoader with fallback loaders by extension."""

        try:
            from langchain_community.document_loaders import (
                Docx2txtLoader,
                PyPDFLoader,
                UnstructuredFileLoader,
            )
        except ModuleNotFoundError as exc:
            raise ResumeExtractionError(
                "langchain-community loaders are required for resume extraction"
            ) from exc

        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(content)
                temp_path = handle.name

            docs = []
            unstructured_error: Exception | None = None
            try:
                docs = UnstructuredFileLoader(temp_path).load()
            except Exception as exc:  # pragma: no cover - fallback path
                unstructured_error = exc

            if not docs:
                if suffix == ".pdf":
                    docs = PyPDFLoader(temp_path).load()
                elif suffix == ".docx":
                    docs = Docx2txtLoader(temp_path).load()
                elif unstructured_error:
                    raise unstructured_error

            return "\n\n".join(
                doc.page_content.strip() for doc in docs if getattr(doc, "page_content", "").strip()
            )
        except Exception as exc:
            raise ResumeExtractionError(f"resume extraction failed: {exc}") from exc
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    @staticmethod
    def _clip_suffix(full_text: str, clipped_text: str) -> str:
        """Append marker when extracted text was truncated."""

        if len(full_text) == len(clipped_text):
            return ""
        return " [TRUNCATED]"
