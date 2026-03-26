"""Background parse processor for application resumes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime, timezone
import logging
import re
from typing import Any
from uuid import UUID

import anyio

from app.core.runtime_config import ApplicationRuntimeConfig, NotificationRuntimeConfig
from app.repositories.application_repository import ApplicationRepository
from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.application import ApplicationRecord
from app.services.email_sender import EmailSender, InitialScreeningRejectionEmail, NoopEmailSender
from app.services.resume_extractor import LangChainResumeExtractor
from app.services.resume_structured_extractor import ResumeStructuredExtractor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedResume:
    """Compact structured parse output stored in one DB JSON column."""

    parsed_at: str
    skills: list[str]
    total_years_experience: float | None
    education: list[dict[str, Any]]
    work_experience: list[dict[str, Any]]
    old_offices: list[str]
    key_achievements: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Convert dataclass result to JSON-serializable dictionary."""

        return {
            "parsed_at": self.parsed_at,
            "skills": self.skills,
            "total_years_experience": self.total_years_experience,
            "education": self.education,
            "work_experience": self.work_experience,
            "old_offices": self.old_offices,
            "key_achievements": self.key_achievements,
        }


class ResumeParseProcessor:
    """Update parse lifecycle fields for queued applications."""

    def __init__(
        self,
        *,
        repository: ApplicationRepository,
        job_opening_repository: JobOpeningRepository,
        application_config: ApplicationRuntimeConfig,
        extractor: LangChainResumeExtractor,
        structured_extractor: ResumeStructuredExtractor,
        llm_fallback_min_chars: int,
        prefilter_max_search_text_chars: int,
        notification_config: NotificationRuntimeConfig | None = None,
        email_sender: EmailSender | None = None,
    ):
        """Initialize with application repository and text extractor."""

        self._repository = repository
        self._job_opening_repository = job_opening_repository
        self._application_config = application_config
        self._extractor = extractor
        self._structured_extractor = structured_extractor
        self._llm_fallback_min_chars = max(100, llm_fallback_min_chars)
        self._prefilter_max_search_text_chars = max(500, prefilter_max_search_text_chars)
        self._notification_config = notification_config or NotificationRuntimeConfig(enabled=False)
        self._email_sender = email_sender or NoopEmailSender()

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
            parsed_total_years_experience=record.parsed_total_years_experience,
            parsed_search_text=record.parsed_search_text,
        )

        try:
            parsed = await self._parse_resume(record)
            parse_result_payload = parsed.to_dict()
            parsed_search_text = self._build_prefilter_search_text(parse_result_payload)
            screening_meta = await self._evaluate_initial_screening(
                record=record,
                parsed_total_years_experience=parsed.total_years_experience,
                parsed_skills=parsed.skills,
                parsed_search_text=parsed_search_text,
            )
            if screening_meta is not None:
                parse_result_payload["initial_screening"] = screening_meta
            await self._repository.update_parse_state(
                application_id=application_id,
                parse_status="completed",
                parse_result=parse_result_payload,
                parsed_total_years_experience=parsed.total_years_experience,
                parsed_search_text=parsed_search_text,
            )
            if screening_meta is not None and self._can_auto_set_initial_status(record):
                passed = bool(screening_meta.get("passed", False))
                await self._repository.update_admin_review(
                    application_id=application_id,
                    updates={
                        "applicant_status": ("screened" if passed else "rejected"),
                        "rejection_reason": (
                            None
                            if passed
                            else self._application_config.initial_screening_fail_reason
                        ),
                    },
                )
                if not passed:
                    await self._send_initial_screening_rejection_email(record)
        except Exception as exc:
            await self._repository.update_parse_state(
                application_id=application_id,
                parse_status="failed",
                parse_result={
                    "error": str(exc),
                    "failed_at": datetime.now(tz=timezone.utc).isoformat(),
                },
                parsed_total_years_experience=None,
                parsed_search_text=None,
            )
            raise

        return True

    async def _parse_resume(self, record: ApplicationRecord) -> ParsedResume:
        """Extract text using LangChain and build compact parse payload."""

        extracted_text = await self._extractor.extract_text(record.resume.storage_path)
        structured = self._structured_extractor.extract(
            text=extracted_text,
            fallback_name=record.full_name,
        )
        structured_dict = structured.to_dict()
        work_experience = self._build_work_experience(
            extracted_text=extracted_text,
            work_history=structured_dict.get("work_history", []),
        )
        return ParsedResume(
            parsed_at=datetime.now(tz=timezone.utc).isoformat(),
            skills=self._normalize_strings(structured_dict.get("skills")),
            total_years_experience=self._resolve_total_years_experience(
                structured_years=structured_dict.get("total_years_experience"),
                work_experience=work_experience,
            ),
            education=self._normalize_dict_list(structured_dict.get("education")),
            work_experience=work_experience,
            old_offices=self._extract_old_offices(work_experience),
            key_achievements=self._extract_key_achievements(extracted_text=extracted_text),
        )

    def _build_work_experience(
        self,
        *,
        extracted_text: str,
        work_history: Any,
    ) -> list[dict[str, Any]]:
        """Build work-experience entries with job descriptions."""

        if not isinstance(work_history, list):
            return []

        source_lines = [line.strip() for line in extracted_text.splitlines() if line.strip()]
        rows: list[dict[str, Any]] = []
        for raw in work_history:
            if not isinstance(raw, dict):
                continue
            company = raw.get("company")
            position = raw.get("position")
            descriptions = self._collect_job_descriptions(
                source_lines=source_lines,
                company=company if isinstance(company, str) else None,
                position=position if isinstance(position, str) else None,
            )
            rows.append(
                {
                    "company": company if isinstance(company, str) else None,
                    "position": position if isinstance(position, str) else None,
                    "start_date": raw.get("start_date"),
                    "end_date": raw.get("end_date"),
                    "duration_years": raw.get("duration_years"),
                    "job_description": descriptions,
                }
            )
        return rows[:60]

    @staticmethod
    def _resolve_total_years_experience(
        *,
        structured_years: Any,
        work_experience: list[dict[str, Any]],
    ) -> float | None:
        """Resolve total years from merged date intervals (no double counting)."""

        intervals: list[tuple[date, date]] = []
        for row in work_experience:
            start_raw = row.get("start_date")
            end_raw = row.get("end_date")
            if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                continue
            try:
                start_date = date.fromisoformat(start_raw)
                end_date = date.fromisoformat(end_raw)
            except ValueError:
                continue
            if end_date < start_date:
                continue
            intervals.append((start_date, end_date))

        if intervals:
            return ResumeParseProcessor._sum_non_overlapping_years(intervals)

        if isinstance(structured_years, (int, float)):
            return round(float(structured_years), 2)

        durations: list[float] = []
        for row in work_experience:
            value = row.get("duration_years")
            if isinstance(value, (int, float)) and value > 0:
                durations.append(float(value))
        if durations:
            return round(sum(durations), 2)
        return None

    @staticmethod
    def _sum_non_overlapping_years(intervals: list[tuple[date, date]]) -> float:
        """Merge overlapping month spans and return total rounded years."""

        spans: list[tuple[int, int]] = []
        for start_date, end_date in intervals:
            start_key = start_date.year * 12 + (start_date.month - 1)
            end_key = end_date.year * 12 + (end_date.month - 1)
            spans.append((start_key, end_key))

        spans.sort(key=lambda item: item[0])
        merged: list[tuple[int, int]] = []
        for start_key, end_key in spans:
            if not merged:
                merged.append((start_key, end_key))
                continue
            prev_start, prev_end = merged[-1]
            if start_key <= prev_end + 1:
                merged[-1] = (prev_start, max(prev_end, end_key))
            else:
                merged.append((start_key, end_key))

        total_months = sum((end_key - start_key + 1) for start_key, end_key in merged)
        return round(total_months / 12.0, 2)

    @staticmethod
    def _normalize_strings(value: Any) -> list[str]:
        """Return cleaned list of non-empty strings."""

        if not isinstance(value, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            text = " ".join(item.split()).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    @staticmethod
    def _normalize_dict_list(value: Any) -> list[dict[str, Any]]:
        """Return list of dictionaries only."""

        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _extract_old_offices(work_experience: list[dict[str, Any]]) -> list[str]:
        """Extract distinct office/company names from work-experience rows."""

        offices: list[str] = []
        seen: set[str] = set()
        for row in work_experience:
            company = row.get("company")
            if not isinstance(company, str):
                continue
            cleaned = " ".join(company.split()).strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            offices.append(cleaned)
        return offices

    def _collect_job_descriptions(
        self,
        *,
        source_lines: list[str],
        company: str | None,
        position: str | None,
    ) -> list[str]:
        """Collect nearby bullet points as job-description snippets."""

        anchor_tokens = [token for token in [company, position] if token]
        if not anchor_tokens:
            return []

        anchor_index: int | None = None
        for index, line in enumerate(source_lines):
            lowered = line.casefold()
            if any(token.casefold() in lowered for token in anchor_tokens):
                anchor_index = index
                break
        if anchor_index is None:
            return []

        descriptions: list[str] = []
        seen: set[str] = set()
        for line in source_lines[anchor_index + 1 : anchor_index + 14]:
            cleaned = re.sub(r"^[\-\*\u2022\·\d\.\)\(]+\s*", "", line).strip()
            if not cleaned:
                continue
            if self._looks_like_section_heading(cleaned):
                break
            if re.search(r"\b(?:\d{4}|present|current)\b", cleaned, flags=re.IGNORECASE):
                if descriptions:
                    break
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            descriptions.append(cleaned)
            if len(descriptions) >= 5:
                break
        return descriptions

    def _extract_key_achievements(self, *, extracted_text: str) -> list[str]:
        """Extract concise achievement lines from resume text."""

        lines = [line.strip() for line in extracted_text.splitlines() if line.strip()]
        achievements: list[str] = []
        seen: set[str] = set()
        triggers = (
            "achieved",
            "delivered",
            "improved",
            "increased",
            "reduced",
            "led",
            "launched",
            "optimized",
            "saved",
            "grew",
            "built",
            "developed",
        )

        for line in lines:
            cleaned = re.sub(r"^[\-\*\u2022\·\d\.\)\(]+\s*", "", line).strip()
            if not cleaned:
                continue
            lowered = cleaned.casefold()
            if not any(trigger in lowered for trigger in triggers):
                continue
            if len(cleaned) < 20 or len(cleaned) > 220:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            achievements.append(cleaned)
            if len(achievements) >= 12:
                break

        return achievements

    def _build_prefilter_search_text(self, parse_result: dict[str, Any]) -> str:
        """Build normalized lightweight text used for keyword prefilter."""

        segments: list[str] = []

        for skill in parse_result.get("skills", []):
            if isinstance(skill, str):
                segments.append(skill)

        for office in parse_result.get("old_offices", []):
            if isinstance(office, str):
                segments.append(office)

        for achievement in parse_result.get("key_achievements", []):
            if isinstance(achievement, str):
                segments.append(achievement)

        for item in parse_result.get("education", []):
            if not isinstance(item, dict):
                continue
            for value in item.values():
                if isinstance(value, str):
                    segments.append(value)

        for item in parse_result.get("work_experience", []):
            if not isinstance(item, dict):
                continue
            for key in ["company", "position"]:
                value = item.get(key)
                if isinstance(value, str):
                    segments.append(value)
            job_description = item.get("job_description")
            if isinstance(job_description, list):
                for row in job_description:
                    if isinstance(row, str):
                        segments.append(row)

        raw_text = " ".join(segments).casefold()
        normalized = " ".join(re.findall(r"[a-z0-9\+\#\.]{2,}", raw_text))
        return normalized[: self._prefilter_max_search_text_chars]

    async def _evaluate_initial_screening(
        self,
        *,
        record: ApplicationRecord,
        parsed_total_years_experience: float | None,
        parsed_skills: list[str],
        parsed_search_text: str,
    ) -> dict[str, Any] | None:
        """Evaluate a lightweight initial screening against opening requirements."""

        opening = await self._job_opening_repository.get(record.job_opening_id)
        if opening is None:
            return None

        min_years, max_years = self._parse_experience_range(opening.experience_range)
        experience_pass = self._is_experience_within_range(
            value=parsed_total_years_experience,
            min_years=min_years,
            max_years=max_years,
        )

        keywords = self._extract_prefilter_keywords(opening.requirements, opening.responsibilities)
        skill_keywords = self._extract_required_skill_keywords(opening.requirements)
        candidate_skills_text = " ".join(self._normalize_strings(parsed_skills)).casefold()
        matched_skills = [item for item in skill_keywords if item in candidate_skills_text]
        required_skill_matches = min(
            len(skill_keywords),
            max(1, self._application_config.prefilter_min_skill_matches),
        )
        skills_pass = True if not skill_keywords else len(matched_skills) >= required_skill_matches

        matched_keywords = [item for item in keywords if item in parsed_search_text]
        required_keyword_matches = min(
            len(keywords),
            max(1, self._application_config.prefilter_min_keyword_matches),
        )
        keyword_pass = True if not keywords else len(matched_keywords) >= required_keyword_matches

        signal_pass = skills_pass or keyword_pass

        return {
            "passed": bool(experience_pass and signal_pass),
            "experience_pass": experience_pass,
            "skills_pass": skills_pass,
            "keyword_pass": keyword_pass,
            "min_years": min_years,
            "max_years": max_years,
            "candidate_years": parsed_total_years_experience,
            "required_skill_matches": required_skill_matches if skill_keywords else 0,
            "matched_skill_count": len(matched_skills),
            "sample_matched_skills": matched_skills[:8],
            "required_keyword_matches": required_keyword_matches if keywords else 0,
            "matched_keyword_count": len(matched_keywords),
            "sample_matched_keywords": matched_keywords[:8],
            "screening_rule": "experience_and_(skills_or_keywords)",
        }

    def _extract_required_skill_keywords(self, requirements: list[str]) -> list[str]:
        """Extract normalized skill-focused keywords from job requirements text."""

        return self._extract_prefilter_keywords(requirements, [])

    def _extract_prefilter_keywords(
        self,
        requirements: list[str],
        responsibilities: list[str],
    ) -> list[str]:
        """Extract stable keywords for quick first-pass matching."""

        stop_words = {item.casefold() for item in self._application_config.prefilter_stop_words}
        source_text = " ".join([*requirements, *responsibilities])
        tokens = re.findall(r"[A-Za-z0-9\+\#\.]{2,}", source_text.casefold())
        keywords: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if len(token) < self._application_config.prefilter_min_keyword_length:
                continue
            if token.isdigit():
                continue
            if token in stop_words:
                continue
            if token in seen:
                continue
            seen.add(token)
            keywords.append(token)
            if len(keywords) >= self._application_config.prefilter_max_keywords:
                break
        return keywords

    @staticmethod
    def _parse_experience_range(experience_range: str) -> tuple[float | None, float | None]:
        """Parse experience range value like '2-4 years' into numeric bounds."""

        years = re.findall(r"\d+", experience_range)
        if len(years) < 2:
            return None, None
        lower = float(years[0])
        upper = float(years[1])
        if lower > upper:
            return upper, lower
        return lower, upper

    @staticmethod
    def _is_experience_within_range(
        *,
        value: float | None,
        min_years: float | None,
        max_years: float | None,
    ) -> bool:
        """Return True when experience falls within parsed bounds."""

        if min_years is None and max_years is None:
            return True
        if value is None:
            return False
        if min_years is not None and value < min_years:
            return False
        if max_years is not None and value > max_years:
            return False
        return True

    @staticmethod
    def _can_auto_set_initial_status(record: ApplicationRecord) -> bool:
        """Return True when candidate status can be auto-updated by parser."""

        return record.applicant_status in {"applied", "received", "in_progress", "pending"}

    async def _send_initial_screening_rejection_email(
        self,
        record: ApplicationRecord,
    ) -> None:
        """Send polite rejection email after initial-screening failure."""

        if not self._notification_config.enabled:
            return
        payload = InitialScreeningRejectionEmail(
            candidate_name=record.full_name,
            candidate_email=str(record.email),
            role_title=record.role_selection,
            rejection_reason=self._application_config.initial_screening_fail_reason,
        )
        try:
            with anyio.fail_after(self._notification_config.send_timeout_seconds):
                await self._email_sender.send_initial_screening_rejection(payload)
        except Exception:
            logger.exception(
                "failed to send initial-screening rejection email",
                extra={"application_id": str(record.id)},
            )

    @staticmethod
    def _looks_like_section_heading(line: str) -> bool:
        """Heuristic check for section-heading style lines."""

        compact = " ".join(line.split()).strip(" :")
        if not compact:
            return False
        if len(compact) > 40:
            return False
        if compact.isupper():
            return True
        keywords = {
            "skills",
            "experience",
            "work experience",
            "employment",
            "education",
            "projects",
            "certifications",
            "summary",
            "profile",
            "references",
        }
        return compact.casefold() in keywords
