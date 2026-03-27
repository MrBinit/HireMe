"""Async service for AI-generated manager offer letters."""

from __future__ import annotations

import json
from typing import Any

import anyio

from app.core.error import ApplicationValidationError
from app.core.runtime_config import (
    BedrockRuntimeConfig,
    EvaluationRuntimeConfig,
)
from app.infra.bedrock_runtime import BedrockInvocationError, BedrockRuntimeClient
from app.schemas.application import ApplicationRecord, ManagerSelectionDetails


class OfferLetterService:
    """Generate professional offer letters from manager inputs + candidate profile."""

    def __init__(
        self,
        *,
        bedrock_client: BedrockRuntimeClient,
        bedrock_config: BedrockRuntimeConfig,
        evaluation_config: EvaluationRuntimeConfig,
    ) -> None:
        """Initialize generator with Bedrock and evaluation config."""

        self._bedrock_client = bedrock_client
        self._bedrock_config = bedrock_config
        self._evaluation_config = evaluation_config

    async def generate_offer_letter(
        self,
        *,
        candidate: ApplicationRecord,
        selection_details: ManagerSelectionDetails,
    ) -> str:
        """Generate one offer letter asynchronously using the secondary model."""

        if not self._evaluation_config.offer_letter_generation_enabled:
            raise ApplicationValidationError("offer letter generation is disabled")
        if not self._bedrock_config.enabled:
            raise ApplicationValidationError("LLM offer letter generation is disabled")

        manager_input = {
            "candidate_name": candidate.full_name,
            "candidate_email": str(candidate.email),
            "role_applied": candidate.role_selection,
            "decision": "select",
            "confirmed_job_title": selection_details.confirmed_job_title,
            "start_date": selection_details.start_date.isoformat(),
            "base_salary": selection_details.base_salary,
            "compensation_structure": selection_details.compensation_structure,
            "equity_or_bonus": selection_details.equity_or_bonus or "-",
            "reporting_manager": selection_details.reporting_manager,
            "custom_terms": selection_details.custom_terms or "-",
        }
        manager_input_json = json.dumps(manager_input, ensure_ascii=True)

        candidate_profile_payload: dict[str, Any] = {
            "id": str(candidate.id),
            "full_name": candidate.full_name,
            "email": str(candidate.email),
            "role_selection": candidate.role_selection,
            "applicant_status": candidate.applicant_status,
            "ai_score": candidate.ai_score,
            "ai_screening_summary": candidate.ai_screening_summary,
            "candidate_brief": candidate.candidate_brief,
            "online_research_summary": candidate.online_research_summary,
            "parse_result": candidate.parse_result if isinstance(candidate.parse_result, dict) else {},
        }
        candidate_profile_json = json.dumps(candidate_profile_payload, ensure_ascii=True)
        candidate_profile_json = self._clip(
            candidate_profile_json,
            max_chars=self._evaluation_config.offer_letter_profile_json_max_chars,
        )

        prompt_template = self._evaluation_config.offer_letter_prompt_template
        if not prompt_template.strip():
            raise ApplicationValidationError("offer_letter_prompt_template is empty")
        prompt = prompt_template.format(
            manager_input_json=manager_input_json,
            candidate_profile_json=candidate_profile_json,
        )
        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._bedrock_config.max_tokens,
            "temperature": 0.0,
            "top_p": self._bedrock_config.top_p,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }
        try:
            with anyio.fail_after(self._bedrock_config.request_timeout_seconds):
                response = await self._bedrock_client.invoke_json(
                    model_id=self._bedrock_config.fallback_model_id,
                    payload=payload,
                )
        except (BedrockInvocationError, TimeoutError) as exc:
            raise ApplicationValidationError(
                f"failed to generate offer letter with secondary model: {exc}"
            ) from exc
        except Exception as exc:  # pragma: no cover - unexpected provider/runtime error
            raise ApplicationValidationError(
                f"failed to generate offer letter with secondary model: {exc}"
            ) from exc

        generated = self._extract_response_text(response).strip()
        if not generated:
            raise ApplicationValidationError("LLM returned empty offer letter")
        return self._clip(generated, max_chars=self._evaluation_config.offer_letter_max_chars)

    @staticmethod
    def _extract_response_text(response: dict[str, Any]) -> str:
        """Extract text content from Bedrock Anthropic response."""

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

        raise ApplicationValidationError("LLM response did not contain offer letter text")

    @staticmethod
    def _clip(value: str, *, max_chars: int) -> str:
        """Clip strings to maximum configured character length."""

        if max_chars <= 0:
            return value
        if len(value) <= max_chars:
            return value
        return value[:max_chars]
