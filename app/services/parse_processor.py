"""Background parse processor for application resumes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.repositories.application_repository import ApplicationRepository
from app.schemas.application import ApplicationRecord
from app.services.resume_extractor import LangChainResumeExtractor
from app.services.resume_structured_extractor import ResumeStructuredExtractor


@dataclass(frozen=True)
class ParsedResume:
    """Structured parse output stored in DB JSON field."""

    parser: str
    extracted_at: str
    file_extension: str
    file_name: str
    storage_path: str
    extracted_text: str
    extracted_characters: int
    llm_fallback_recommended: bool
    structured: dict[str, Any]
    llm_input_hint: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert dataclass result to JSON-serializable dictionary."""

        return {
            "parser": self.parser,
            "extracted_at": self.extracted_at,
            "file_extension": self.file_extension,
            "file_name": self.file_name,
            "storage_path": self.storage_path,
            "extracted_text": self.extracted_text,
            "extracted_characters": self.extracted_characters,
            "llm_fallback_recommended": self.llm_fallback_recommended,
            "structured": self.structured,
            "llm_input_hint": self.llm_input_hint,
        }


class ResumeParseProcessor:
    """Update parse lifecycle fields for queued applications."""

    def __init__(
        self,
        *,
        repository: ApplicationRepository,
        extractor: LangChainResumeExtractor,
        structured_extractor: ResumeStructuredExtractor,
        llm_fallback_min_chars: int,
    ):
        """Initialize with application repository and text extractor."""

        self._repository = repository
        self._extractor = extractor
        self._structured_extractor = structured_extractor
        self._llm_fallback_min_chars = max(100, llm_fallback_min_chars)

    async def process(self, application_id: UUID) -> bool:
        """Process one application and return False when record does not exist."""

        record = await self._repository.get_by_id(application_id)
        if record is None:
            return False

        if record.parse_status == "completed":
            return True

        await self._repository.update_parse_state(
            application_id=application_id,
            parse_status="in_progress",
            parse_result=record.parse_result,
        )

        try:
            parsed = await self._parse_resume(record)
            await self._repository.update_parse_state(
                application_id=application_id,
                parse_status="completed",
                parse_result=parsed.to_dict(),
            )
        except Exception as exc:
            await self._repository.update_parse_state(
                application_id=application_id,
                parse_status="failed",
                parse_result={
                    "error": str(exc),
                    "failed_at": datetime.now(tz=timezone.utc).isoformat(),
                },
            )
            raise

        return True

    async def _parse_resume(self, record: ApplicationRecord) -> ParsedResume:
        """Extract text using LangChain and build parse payload."""

        file_extension = Path(record.resume.stored_filename).suffix.lower()
        extracted_text = await self._extractor.extract_text(record.resume.storage_path)
        extracted_characters = len(extracted_text)
        structured = self._structured_extractor.extract(
            text=extracted_text,
            fallback_name=record.full_name,
        )
        structured_dict = structured.to_dict()
        llm_fallback_recommended = self._should_recommend_llm_fallback(
            extracted_characters=extracted_characters,
            structured=structured_dict,
        )
        return ParsedResume(
            parser="langchain_unstructured",
            extracted_at=datetime.now(tz=timezone.utc).isoformat(),
            file_extension=file_extension,
            file_name=record.resume.original_filename,
            storage_path=record.resume.storage_path,
            extracted_text=extracted_text,
            extracted_characters=extracted_characters,
            llm_fallback_recommended=llm_fallback_recommended,
            structured=structured_dict,
            llm_input_hint=self._build_llm_input_hint(
                extracted_text=extracted_text,
                structured=structured_dict,
            ),
        )

    def _should_recommend_llm_fallback(
        self,
        *,
        extracted_characters: int,
        structured: dict,
    ) -> bool:
        """Return True when structured extraction quality is likely low."""

        if extracted_characters < self._llm_fallback_min_chars:
            return True
        if not structured.get("skills"):
            return True
        if not structured.get("work_history"):
            return True
        return False

    @staticmethod
    def _build_llm_input_hint(*, extracted_text: str, structured: dict) -> dict:
        """Build compact LLM-ready context from parser output."""

        return {
            "priority_fields": {
                "skills": structured.get("skills", []),
                "projects": structured.get("projects", []),
                "position": structured.get("position"),
                "work_history": structured.get("work_history", []),
                "total_years_experience": structured.get("total_years_experience"),
                "education": structured.get("education", []),
            },
            "raw_text_excerpt": extracted_text[:3000],
        }
