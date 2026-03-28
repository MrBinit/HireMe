"""LLM-based candidate evaluation against job opening requirements."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import UUID

import anyio

from app.core.error import ApplicationValidationError
from app.core.runtime_config import (
    ApplicationRuntimeConfig,
    BedrockRuntimeConfig,
    EvaluationRuntimeConfig,
)
from app.infra.bedrock_runtime import BedrockInvocationError, BedrockRuntimeClient
from app.repositories.application_repository import ApplicationRepository
from app.repositories.job_opening_repository import JobOpeningRepository
from app.schemas.evaluation import CandidateEvaluationResult


class CandidateEvaluationService:
    """Service for calling Bedrock and returning validated score output."""

    def __init__(
        self,
        *,
        application_repository: ApplicationRepository,
        job_opening_repository: JobOpeningRepository,
        bedrock_client: BedrockRuntimeClient,
        bedrock_config: BedrockRuntimeConfig,
        evaluation_config: EvaluationRuntimeConfig,
        application_config: ApplicationRuntimeConfig,
    ) -> None:
        """Initialize evaluator dependencies and concurrency guard."""

        self._application_repository = application_repository
        self._job_opening_repository = job_opening_repository
        self._bedrock_client = bedrock_client
        self._bedrock_config = bedrock_config
        self._evaluation_config = evaluation_config
        self._application_config = application_config
        self._semaphore = asyncio.Semaphore(max(1, bedrock_config.max_concurrency))

    async def evaluate_application(
        self,
        *,
        application_id: UUID,
    ) -> CandidateEvaluationResult:
        """Evaluate one candidate profile against the mapped job opening."""

        candidate, opening = await self._load_candidate_and_opening(application_id=application_id)
        must_have_skills, nice_to_have_skills = self._split_requirements_by_priority(
            opening.requirements
        )
        required_skills = [*must_have_skills, *nice_to_have_skills]

        prompt = self._build_prompt(
            candidate_parse_result=candidate.parse_result,
            work_summary=await self._summarize_work_history_with_fallback_model(
                candidate.parse_result.get("work_experience")
            ),
            role=opening.role_title,
            required_skills=required_skills,
            must_have_skills=must_have_skills,
            nice_to_have_skills=nice_to_have_skills,
            min_exp=self._parse_experience_range(opening.experience_range)[0],
            max_exp=self._parse_experience_range(opening.experience_range)[1],
            job_description="; ".join([*opening.responsibilities, *opening.requirements]),
            years=candidate.parsed_total_years_experience,
        )

        return await self._invoke_and_parse(
            model_id=self._bedrock_config.primary_model_id,
            prompt=prompt,
        )

    async def validate_candidate_for_evaluation(
        self,
        *,
        application_id: UUID,
    ) -> None:
        """Validate candidate readiness for evaluation without invoking LLM."""

        await self._load_candidate_and_opening(application_id=application_id)

    async def _load_candidate_and_opening(
        self,
        *,
        application_id: UUID,
    ) -> tuple[Any, Any]:
        """Load and validate candidate + opening prerequisites for LLM evaluation."""

        if not self._bedrock_config.enabled or not self._evaluation_config.enabled:
            raise ApplicationValidationError("LLM evaluation is disabled")

        candidate = await self._application_repository.get_by_id(application_id)
        if candidate is None:
            raise ApplicationValidationError("candidate application not found")
        if candidate.parse_status != "completed":
            raise ApplicationValidationError("candidate parse is not completed yet")
        if not isinstance(candidate.parse_result, dict):
            raise ApplicationValidationError("candidate parse_result is not available")
        if not self._is_initial_screening_passed(candidate):
            raise ApplicationValidationError(
                "candidate failed initial screening; LLM scoring is not allowed"
            )

        opening = await self._job_opening_repository.get(candidate.job_opening_id)
        if opening is None:
            raise ApplicationValidationError("job opening not found for candidate")
        return candidate, opening

    @staticmethod
    def _is_initial_screening_passed(candidate: Any) -> bool:
        """Return True when candidate has passed the initial screening gate."""

        if not isinstance(candidate.parse_result, dict):
            return False

        initial = candidate.parse_result.get("initial_screening")
        if isinstance(initial, dict):
            passed = initial.get("passed")
            if isinstance(passed, bool):
                return passed

        # Backward compatibility for older records before initial_screening metadata existed.
        return candidate.applicant_status in {
            "screened",
            "shortlisted",
            "in_interview",
            "offer",
            "accepted",
        }

    async def _invoke_and_parse(
        self,
        *,
        model_id: str,
        prompt: str,
    ) -> CandidateEvaluationResult:
        """Invoke one model and parse strict JSON scoring response."""

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._bedrock_config.max_tokens,
            "temperature": self._bedrock_config.temperature,
            "top_p": self._bedrock_config.top_p,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        async with self._semaphore:
            try:
                with anyio.fail_after(self._bedrock_config.request_timeout_seconds):
                    response = await self._bedrock_client.invoke_json(
                        model_id=model_id,
                        payload=payload,
                    )
            except (TimeoutError, BedrockInvocationError) as exc:
                raise ApplicationValidationError(str(exc)) from exc
            except Exception as exc:  # pragma: no cover - unexpected runtime/provider error
                raise ApplicationValidationError(str(exc)) from exc

        response_text = self._extract_response_text(response)
        parsed_payload = self._extract_first_json_object(response_text)
        result = CandidateEvaluationResult.model_validate(parsed_payload)
        if len(result.reason) > self._evaluation_config.max_reason_chars:
            result = result.model_copy(
                update={"reason": result.reason[: self._evaluation_config.max_reason_chars]}
            )
        return result

    async def _summarize_work_history_with_fallback_model(self, raw_work: Any) -> str:
        """Use fallback model for work-history summarization before scoring."""

        fallback_summary = self._render_work_summary(raw_work)
        if not isinstance(raw_work, list):
            return fallback_summary

        prompt_template = self._evaluation_config.summary_prompt_template.strip()
        if not prompt_template:
            return fallback_summary

        work_history_json = json.dumps(self._normalize_work_history(raw_work), ensure_ascii=True)
        summary_prompt = prompt_template.replace("{work_history_json}", work_history_json)

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": min(self._bedrock_config.max_tokens, 600),
            "temperature": 0.1,
            "top_p": self._bedrock_config.top_p,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": summary_prompt}],
                }
            ],
        }

        try:
            async with self._semaphore:
                with anyio.fail_after(self._bedrock_config.request_timeout_seconds):
                    response = await self._bedrock_client.invoke_json(
                        model_id=self._bedrock_config.fallback_model_id,
                        payload=payload,
                    )
            summary_text = self._extract_response_text(response)
            compact_summary = " ".join(summary_text.split()).strip()
            if compact_summary:
                return compact_summary[: self._evaluation_config.max_work_summary_chars]
        except Exception:
            return fallback_summary

        return fallback_summary

    @staticmethod
    def _extract_response_text(response: dict[str, Any]) -> str:
        """Extract assistant text from Bedrock Anthropic response shape."""

        content = response.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text_value = item.get("text")
                if isinstance(text_value, str):
                    parts.append(text_value)
            joined = "\n".join(parts).strip()
            if joined:
                return joined

        output_text = response.get("outputText")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        raise ApplicationValidationError("LLM response did not contain text output")

    @staticmethod
    def _extract_first_json_object(text_value: str) -> dict[str, Any]:
        """Return first valid JSON object found in model output text."""

        decoder = json.JSONDecoder()
        for index, char in enumerate(text_value):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text_value[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise ApplicationValidationError("LLM output is not strict JSON")

    def _build_prompt(
        self,
        *,
        candidate_parse_result: dict[str, Any],
        work_summary: str,
        role: str,
        required_skills: list[str],
        must_have_skills: list[str],
        nice_to_have_skills: list[str],
        min_exp: int | None,
        max_exp: int | None,
        job_description: str,
        years: float | None,
    ) -> str:
        """Render evaluation prompt from YAML template and candidate/job context."""

        template = self._evaluation_config.prompt_template.strip()
        if not template:
            raise ApplicationValidationError("evaluation prompt_template is empty")

        skills = self._render_skills(candidate_parse_result.get("skills"))
        education = self._render_education(candidate_parse_result.get("education"))
        required_skills_text = ", ".join(required_skills) if required_skills else "not available"
        must_have_skills_text = ", ".join(must_have_skills) if must_have_skills else "not available"
        nice_to_have_skills_text = (
            ", ".join(nice_to_have_skills) if nice_to_have_skills else "not available"
        )

        replacements = {
            "{skills}": skills,
            "{years}": str(years) if years is not None else "unknown",
            "{work_summary}": work_summary,
            "{education}": education,
            "{role}": role or "not available",
            "{required_skills}": required_skills_text,
            "{must_have_skills}": must_have_skills_text,
            "{nice_to_have_skills}": nice_to_have_skills_text,
            "{min_exp}": str(min_exp) if min_exp is not None else "unknown",
            "{max_exp}": str(max_exp) if max_exp is not None else "unknown",
            "{job_description}": job_description or "not available",
        }

        prompt = template
        for key, value in replacements.items():
            prompt = prompt.replace(key, value)
        return prompt

    @staticmethod
    def _normalize_work_history(raw_work: list[Any]) -> list[dict[str, Any]]:
        """Normalize raw work-history objects before sending to summary model."""

        rows: list[dict[str, Any]] = []
        for item in raw_work[:10]:
            if not isinstance(item, dict):
                continue
            row: dict[str, Any] = {}
            for key in ["position", "company", "start_date", "end_date", "duration_years"]:
                value = item.get(key)
                if isinstance(value, (str, int, float)):
                    row[key] = value
            descriptions = item.get("job_description")
            if isinstance(descriptions, list):
                row["job_description"] = [
                    text for text in descriptions[:3] if isinstance(text, str) and text.strip()
                ]
            rows.append(row)
        return rows

    @staticmethod
    def _render_skills(raw_skills: Any) -> str:
        """Render candidate skills for prompt context."""

        if not isinstance(raw_skills, list):
            return "not available"
        values = [item.strip() for item in raw_skills if isinstance(item, str) and item.strip()]
        return ", ".join(values) if values else "not available"

    @staticmethod
    def _render_work_summary(raw_work: Any) -> str:
        """Render compact work-history summary text for prompt context."""

        if not isinstance(raw_work, list):
            return "not available"
        chunks: list[str] = []
        for row in raw_work[:8]:
            if not isinstance(row, dict):
                continue
            position = row.get("position") if isinstance(row.get("position"), str) else None
            company = row.get("company") if isinstance(row.get("company"), str) else None
            line = " at ".join([item for item in [position, company] if item]) or "Unknown role"
            descriptions = row.get("job_description")
            if isinstance(descriptions, list):
                bullet = "; ".join(
                    [item for item in descriptions if isinstance(item, str) and item.strip()][:2]
                )
                if bullet:
                    line = f"{line}: {bullet}"
            chunks.append(line)
        return " | ".join(chunks) if chunks else "not available"

    @staticmethod
    def _render_education(raw_education: Any) -> str:
        """Render compact education summary text for prompt context."""

        if not isinstance(raw_education, list):
            return "not available"
        values: list[str] = []
        for row in raw_education[:5]:
            if not isinstance(row, dict):
                continue
            parts = [
                str(value).strip()
                for value in row.values()
                if isinstance(value, str) and str(value).strip()
            ]
            if parts:
                values.append(", ".join(parts))
        return " | ".join(values) if values else "not available"

    @staticmethod
    def _parse_experience_range(value: str) -> tuple[int | None, int | None]:
        """Parse range string `2-4 years` into integer bounds."""

        years = re.findall(r"\d+", value)
        if len(years) < 2:
            return None, None
        lower = int(years[0])
        upper = int(years[1])
        if lower > upper:
            return upper, lower
        return lower, upper

    @staticmethod
    def _split_requirements_by_priority(requirements: list[str]) -> tuple[list[str], list[str]]:
        """Split requirement bullets into must-have and nice-to-have lists."""

        must_have: list[str] = []
        nice_to_have: list[str] = []

        for raw in requirements:
            if not isinstance(raw, str):
                continue
            cleaned = " ".join(raw.split()).strip()
            if not cleaned:
                continue

            label_match = re.match(
                r"^(must(?:\s+have)?|required|requirement|required skill|"
                r"nice(?:\s+to\s+have)?|preferred|optional|plus)\s*[:\-]\s*(.+)$",
                cleaned,
                flags=re.IGNORECASE,
            )
            if label_match:
                label = label_match.group(1).casefold()
                content = " ".join(label_match.group(2).split()).strip()
                if not content:
                    continue
                if label in {"nice", "nice to have", "preferred", "optional", "plus"}:
                    nice_to_have.append(content)
                else:
                    must_have.append(content)
                continue

            lowered = cleaned.casefold()
            if re.match(r"^(nice(?:\s+to\s+have)?|preferred|optional|plus)\b", lowered):
                content = re.sub(
                    r"^(nice(?:\s+to\s+have)?|preferred|optional|plus)\b",
                    "",
                    cleaned,
                    flags=re.IGNORECASE,
                ).lstrip(" :-")
                nice_to_have.append(content or cleaned)
                continue
            if re.match(r"^(must(?:\s+have)?|required|requirement|required skill)\b", lowered):
                content = re.sub(
                    r"^(must(?:\s+have)?|required|requirement|required skill)\b",
                    "",
                    cleaned,
                    flags=re.IGNORECASE,
                ).lstrip(" :-")
                must_have.append(content or cleaned)
                continue

            must_have.append(cleaned)

        return must_have, nice_to_have

    @staticmethod
    def format_evaluation_summary(result: CandidateEvaluationResult) -> str:
        """Format human-readable AI scoring summary persisted on candidate row."""

        evidence = result.evidence
        evidence_summary = (
            f"evidence(skills={evidence.skills[0]}, "
            f"experience={evidence.experience[0]}, "
            f"education={evidence.education[0]}, "
            f"role_alignment={evidence.role_alignment[0]})"
        )
        return (
            f"{result.reason} "
            f"(confidence={result.confidence:.2f}, "
            f"model_review={result.needs_human_review}, "
            f"(skills={result.breakdown.skills}/40, "
            f"experience={result.breakdown.experience}/30, "
            f"education={result.breakdown.education}/10, "
            f"role_alignment={result.breakdown.role_alignment}/20), "
            f"{evidence_summary})"
        )
